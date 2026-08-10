"""Who earns the night and who pays the day, by how retail a stock is.

The tug-of-war literature (Lou-Polk-Skouras JFE 2019) splits close-to-close
returns at the open: overnight returns are systematically positive and
intraday returns negative for exactly the stocks retail crowds into —
attention names open rich (Berkman et al. JFQA 2012) because retail market
orders cluster at the open, and institutions fade them through the day. The
Barclays retail-flow work says the same crowd buys opens and dips. If the
split is real and still alive in 2024-2026, an automated account that can
place MOO/MOC auction orders harvests it directly: hold the speculative
basket overnight only — and never buy its open.

Universe: the pead-llm-gate study's daily OHLC panels — the top ~1,000
names by dollar volume per year, 2016-2026, ~2.7M symbol-days, all
Adjustment.ALL so every ratio here is basis-consistent. Membership is
year-contemporaneous (the 2019 panel is 2019's liquid names, not today's),
which removes most survivorship; what remains is that a symbol delisted
mid-year stops contributing rows (its last night is still counted).

Speculativeness ("retailness") per symbol-month, from trailing-21-session
data only, assigned to the FOLLOWING month (no lookahead):
    - low price      rank of 1 / close
    - realised vol   rank of std(daily close-to-close)
    - lottery MAX    rank of max daily return (Bali-Cakici-Whitelaw)
Composite = mean of the three percentile ranks; deciles within month.
D10 = most speculative. Eligibility: >= 15 trailing sessions, prior close
>= $1, prior-month median dollar volume >= $10M.

Night = O_t / C_{t-1} - 1 (gap over the close, includes the open auction);
Day = C_t / O_t - 1. Panel gaps > 5 calendar days void the night leg (a
symbol re-entering the panel after months would otherwise book the whole
absence as one night).

Costs for the tradeable read: an overnight-only basket pays two auction
crossings per day. MOO/MOC on liquid names ~1-3bp per side; the note
reports gross and a 4bp/day and 6bp/day net line.

Run:  python scripts/research_tug_of_war.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
NOTES = STUDY / "notes"

ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

MEME = ["GME", "AMC", "BBBY", "BB", "NOK", "PLTR", "TSLA", "COIN", "HOOD",
        "MARA", "RIOT", "SOFI", "RIVN", "LCID", "NIO", "DKNG", "MSTR",
        "SMCI", "DJT", "AI", "IONQ", "CVNA", "UPST", "AFRM", "RBLX"]


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def load_panel() -> pd.DataFrame:
    frames = [pd.read_parquet(p, columns=["symbol", "date", "o", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = (df.drop_duplicates(subset=["symbol", "date"])
          .sort_values(["symbol", "date"]).reset_index(drop=True))
    g = df.groupby("symbol", sort=False)
    df["c_prev"] = g["c"].shift(1)
    df["dt"] = pd.to_datetime(df["date"])
    df["gap_days"] = (df["dt"] - g["dt"].shift(1)).dt.days
    df["cc"] = df["c"] / df["c_prev"] - 1
    df["night"] = np.where(df["gap_days"] <= 5, df["o"] / df["c_prev"] - 1, np.nan)
    df["day"] = df["c"] / df["o"] - 1
    df["dv"] = df["c"] * df["v"]
    df["month"] = df["date"].str[:7]
    return df


def month_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Per (symbol, month): trailing stats of the PRIOR month, ranked within
    month into a composite speculativeness decile."""
    g = df.groupby("symbol", sort=False)
    df = df.assign(
        vol21=g["cc"].transform(lambda s: s.rolling(21, min_periods=15).std()),
        max21=g["cc"].transform(lambda s: s.rolling(21, min_periods=15).max()),
        dv21=g["dv"].transform(lambda s: s.rolling(21, min_periods=15).median()),
    )
    last = (df.groupby(["symbol", "month"], sort=False)
            .agg(sig_price=("c", "last"), sig_vol=("vol21", "last"),
                 sig_max=("max21", "last"), sig_dv=("dv21", "last"))
            .reset_index())
    # shift each symbol's signals one month forward: signals OF month m
    # apply IN the next month the symbol appears.
    last = last.sort_values(["symbol", "month"])
    for col in ("sig_price", "sig_vol", "sig_max", "sig_dv"):
        last[col] = last.groupby("symbol", sort=False)[col].shift(1)
    last = last.dropna(subset=["sig_price", "sig_vol", "sig_max", "sig_dv"])
    last = last[(last["sig_price"] >= 1.0) & (last["sig_dv"] >= 1e7)]
    r_price = last.groupby("month")["sig_price"].rank(pct=True, ascending=False)
    r_vol = last.groupby("month")["sig_vol"].rank(pct=True)
    r_max = last.groupby("month")["sig_max"].rank(pct=True)
    last["spec"] = (r_price + r_vol + r_max) / 3
    last["decile"] = np.clip(np.ceil(last.groupby("month")["spec"]
                                     .rank(pct=True) * 10), 1, 10).astype(int)
    return last[["symbol", "month", "decile"]]


def main() -> None:
    df = load_panel()
    sig = month_signals(df)
    m = df.merge(sig, on=["symbol", "month"], how="inner")
    m = m.dropna(subset=["night", "day"])

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Tug of war: night vs day by speculativeness decile — {STAMP}")
    emit()
    emit(f"{len(m):,} symbol-days across {m['month'].nunique()} months, "
         f"{m['symbol'].nunique()} symbols; deciles re-formed monthly from "
         "prior-month price / realised vol / lottery-MAX ranks (D10 = most "
         "speculative). GROSS of costs; an overnight-only basket pays ~2 "
         "auction crossings per day (2-6bp/day depending on venue quality).")
    emit()

    daily = (m.groupby(["date", "decile"])
             .agg(night=("night", "mean"), day=("day", "mean"),
                  names=("symbol", "size"))
             .reset_index())

    def block(title, dsub):
        emit(f"## {title}")
        emit()
        emit("| decile | night bp/d | t | day bp/d | t | cc bp/d | ann night % | avg names |")
        emit("|---|---|---|---|---|---|---|---|")
        for dec in range(1, 11):
            s = dsub[dsub["decile"] == dec]
            if not len(s):
                continue
            nights = s["night"].to_numpy() * 1e4
            days = s["day"].to_numpy() * 1e4
            emit(f"| D{dec} | {nights.mean():+.1f} | {tstat(nights):+.1f} "
                 f"| {days.mean():+.1f} | {tstat(days):+.1f} "
                 f"| {(nights + days).mean():+.1f} "
                 f"| {nights.mean() * 252 / 100:+.1f} "
                 f"| {s['names'].mean():.0f} |")
        emit()

    block("Full period 2016-2026", daily)
    block("2016-2020", daily[daily["date"] < "2021-01-01"])
    block("2021-2026", daily[daily["date"] >= "2021-01-01"])

    # The trade: D10 overnight-only, and the D10-D1 night spread.
    d10 = daily[daily["decile"] == 10].set_index("date")["night"] * 1e4
    d1 = daily[daily["decile"] == 1].set_index("date")["night"] * 1e4
    spread = (d10 - d1).dropna()
    emit("## The tradeable lines")
    emit()
    emit("| line | bp/day | t | ann gross % | ann net @4bp/d | ann net @6bp/d |")
    emit("|---|---|---|---|---|---|")
    for label, s, cost in (("long D10 overnight only", d10, True),
                           ("D10-D1 night spread", spread, True),
                           ("long D10 intraday (the leg retail pays)",
                            daily[daily["decile"] == 10].set_index("date")["day"] * 1e4,
                            True)):
        x = s.to_numpy()
        gross = x.mean() * 252 / 100
        emit(f"| {label} | {x.mean():+.1f} | {tstat(x):+.1f} | {gross:+.1f} "
             f"| {gross - 252 * 4 / 100:+.1f} | {gross - 252 * 6 / 100:+.1f} |")
    emit()

    # Yearly stability of the D10 night premium.
    d10y = daily[daily["decile"] == 10].copy()
    d10y["year"] = d10y["date"].str[:4]
    emit("## D10 by year (night bp/day / day bp/day)")
    emit()
    emit("| year | night | t | day | t |")
    emit("|---|---|---|---|---|")
    for y, s in d10y.groupby("year"):
        emit(f"| {y} | {s['night'].mean() * 1e4:+.1f} | "
             f"{tstat(s['night'].to_numpy() * 1e4):+.1f} | "
             f"{s['day'].mean() * 1e4:+.1f} | "
             f"{tstat(s['day'].to_numpy() * 1e4):+.1f} |")
    emit()

    # Meme basket, named and survivorship-flagged.
    mm = m[m["symbol"].isin(MEME)]
    if len(mm):
        mdaily = (mm.groupby("date")
                  .agg(night=("night", "mean"), day=("day", "mean"),
                       names=("symbol", "size")).reset_index())
        nights = mdaily["night"].to_numpy() * 1e4
        days = mdaily["day"].to_numpy() * 1e4
        emit(f"## Static meme basket ({mm['symbol'].nunique()} names present; "
             "SURVIVORSHIP-BIASED, colour only)")
        emit()
        emit(f"night {nights.mean():+.1f}bp/d (t {tstat(nights):+.1f}), "
             f"day {days.mean():+.1f}bp/d (t {tstat(days):+.1f}), "
             f"avg {mdaily['names'].mean():.1f} names/day.")
        emit()

    # SPY reference from the same panel.
    spy = df[df["symbol"] == "SPY"].dropna(subset=["night", "day"])
    emit(f"SPY reference: night {spy['night'].mean() * 1e4:+.1f}bp/d "
         f"(t {tstat(spy['night'].to_numpy() * 1e4):+.1f}), day "
         f"{spy['day'].mean() * 1e4:+.1f}bp/d "
         f"(t {tstat(spy['day'].to_numpy() * 1e4):+.1f}) over the same span.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"tug_of_war_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
