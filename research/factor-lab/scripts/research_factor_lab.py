"""Auto-rebalanced fundamental investing across the whole liquid book —
the breadth a person cannot do by hand, priced honestly.

Matthew's ask: value investing across ~all large caps, auto-rebalanced.
The machine's edge here is breadth and discipline, not information: 500
names re-ranked monthly, positions no human portfolio review would keep
straight. The question this study answers is whether that breadth EARNS
anything after the 2016-2026 tape, measured with the discipline the rest
of the research tree demands:

  POINT-IN-TIME FUNDAMENTALS. Every fundamental comes from SEC XBRL
  companyfacts with its FILED date, and a fact enters the signal only
  after it was filed. No survivorship refresh, no restated history: what
  was knowable on rebalance day is all the rank sees. (The universe is
  the top-500-by-dollar-volume of each month from the study panels, which
  is itself near-point-in-time: 2019's names, not today's.)

Factors, long-only top decile (50 names), equal weight, monthly:
  value     E/P: trailing-twelve-month net income / market cap
  bookval   B/P: latest stockholders' equity / market cap
  quality   ROE: TTM net income / equity
  momentum  12-1: prior 12 months excluding the last (prices only)
  combo     mean of the four percentile ranks

Costs: 10bp per side x measured turnover, charged monthly. Benchmark:
SPY on the same adjusted-close basis.

Stages:
  fetch   slim per-CIK facts from data.sec.gov companyfacts (~10/s cap)
  run     the backtest, entirely offline once facts are cached

Run:  python scripts/research_factor_lab.py fetch
      python scripts/research_factor_lab.py run
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
CACHE = STUDY / "cache"
NOTES = STUDY / "notes"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
SEC_UA = {"User-Agent": "planetaria/0.1 (contact: matthewguo.x86@gmail.com)"}

TOP_N = 500            # the monthly universe
DECILE = 50            # long book size
COST_BP_SIDE = 10.0

FACTS_F = CACHE / "slim_facts.parquet"

NI_TAGS = ("NetIncomeLoss",
           "NetIncomeLossAvailableToCommonStockholdersBasic",
           "ProfitLoss")
EQ_TAGS = ("StockholdersEquity",
           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
SH_TAG = "EntityCommonStockSharesOutstanding"


def month_panel() -> pd.DataFrame:
    """Month-end close and trailing dollar volume per symbol from the study
    panels (Adjustment.ALL, so monthly chains include distributions)."""
    frames = [pd.read_parquet(p, columns=["symbol", "date", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = (df.drop_duplicates(subset=["symbol", "date"])
          .sort_values(["symbol", "date"]))
    df["dv"] = df["c"] * df["v"]
    df["month"] = df["date"].str[:7]
    g = df.groupby(["symbol", "month"])
    out = g.agg(close=("c", "last"), dv=("dv", "median"),
                last_day=("date", "last")).reset_index()
    return out


def cik_map() -> dict[str, int]:
    frames = [pd.read_parquet(p) for p in sorted(PEAD_CACHE.glob("universe_*.parquet"))]
    u = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ticker"])
    return dict(zip(u["ticker"], u["cik"].astype(int)))


def stage_fetch(args) -> None:
    import httpx

    CACHE.mkdir(parents=True, exist_ok=True)
    mp = month_panel()
    mp["rank"] = mp.groupby("month")["dv"].rank(ascending=False)
    wanted = sorted(set(mp[mp["rank"] <= TOP_N]["symbol"]))
    cmap = cik_map()
    todo = [(s, cmap[s]) for s in wanted if s in cmap]
    print(f"{len(wanted)} symbols ever in the monthly top-{TOP_N}; "
          f"{len(todo)} have CIKs")

    have: set[int] = set()
    prior = pd.DataFrame()
    if FACTS_F.exists():
        prior = pd.read_parquet(FACTS_F)
        have = set(prior["cik"].unique())
    rows: list[dict] = []
    with httpx.Client(headers=SEC_UA, timeout=30, follow_redirects=True) as http:
        for n, (sym, cik) in enumerate(todo):
            if cik in have:
                continue
            try:
                r = http.get(f"https://data.sec.gov/api/xbrl/companyfacts/"
                             f"CIK{cik:010d}.json")
                if r.status_code != 200:
                    continue
                facts = r.json()
            except (httpx.HTTPError, json.JSONDecodeError):
                _time.sleep(0.5)
                continue
            gaap = (facts.get("facts") or {}).get("us-gaap") or {}
            dei = (facts.get("facts") or {}).get("dei") or {}

            def keep(tag_obj, tag, kind):
                for unit, items in (tag_obj.get("units") or {}).items():
                    if unit not in ("USD", "shares"):
                        continue
                    for it in items:
                        if not it.get("filed") or it.get("val") is None:
                            continue
                        rows.append({
                            "cik": cik, "symbol": sym, "tag": tag,
                            "kind": kind, "start": it.get("start"),
                            "end": it.get("end"), "filed": it["filed"],
                            "val": float(it["val"]),
                        })

            for tag in NI_TAGS:
                if tag in gaap:
                    keep(gaap[tag], "NI", "duration")
                    break
            for tag in EQ_TAGS:
                if tag in gaap:
                    keep(gaap[tag], "EQ", "instant")
                    break
            if SH_TAG in dei:
                keep(dei[SH_TAG], "SH", "instant")
            if n % 50 == 0:
                print(f"  {n}/{len(todo)} ({len(rows)} rows)")
            _time.sleep(0.12)
    df = pd.DataFrame(rows)
    allf = (pd.concat([prior, df], ignore_index=True)
            .drop_duplicates(subset=["cik", "tag", "start", "end", "filed"])
            if len(prior) else df)
    allf.to_parquet(FACTS_F)
    print(f"cached {len(allf)} slim facts for "
          f"{allf['cik'].nunique()} CIKs -> {FACTS_F}")


# ---------------------------------------------------------------------- run

def build_fundamentals(facts: pd.DataFrame) -> dict[str, dict]:
    """Per symbol: sorted arrays of (filed, value) for TTM net income,
    equity, and shares — evaluated later with searchsorted at each
    rebalance date, so nothing filed after the date is visible."""
    out: dict[str, dict] = {}
    facts["filed"] = facts["filed"].astype(str)
    for sym, g in facts.groupby("symbol"):
        rec: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        ni = g[g["tag"] == "NI"].dropna(subset=["start", "end"]).copy()
        if len(ni):
            ni["days"] = ((pd.to_datetime(ni["end"])
                           - pd.to_datetime(ni["start"])).dt.days)
            q = ni[(ni["days"] >= 70) & (ni["days"] <= 100)]
            q = (q.sort_values("filed").drop_duplicates(subset=["end"], keep="first")
                 .sort_values("end"))
            ttm_filed, ttm_val = [], []
            ends = q["end"].to_numpy()
            vals = q["val"].to_numpy()
            fils = q["filed"].to_numpy()
            for i in range(3, len(q)):
                # TTM known when the LAST of the four quarters is filed;
                # the four must be consecutive-ish (span <= ~400 days).
                span = (pd.Timestamp(ends[i]) - pd.Timestamp(ends[i - 3])).days
                if span <= 300:
                    ttm_filed.append(max(fils[i - 3:i + 1]))
                    ttm_val.append(vals[i - 3:i + 1].sum())
            if ttm_filed:
                order = np.argsort(ttm_filed)
                rec["ni_ttm"] = (np.array(ttm_filed)[order],
                                 np.array(ttm_val)[order])
        for tag, key in (("EQ", "equity"), ("SH", "shares")):
            t = (g[g["tag"] == tag].sort_values(["filed", "end"])
                 .drop_duplicates(subset=["filed"], keep="last"))
            if len(t):
                rec[key] = (t["filed"].to_numpy(), t["val"].to_numpy())
        if rec:
            out[sym] = rec
    return out


def asof(rec: dict, key: str, date: str) -> float | None:
    got = rec.get(key)
    if got is None:
        return None
    filed, val = got
    j = np.searchsorted(filed, date, side="right") - 1
    return float(val[j]) if j >= 0 else None


def stage_run(args) -> None:
    if not FACTS_F.exists():
        raise SystemExit("run `fetch` first")
    facts = pd.read_parquet(FACTS_F)
    fund = build_fundamentals(facts)
    mp = month_panel()
    mp["rank"] = mp.groupby("month")["dv"].rank(ascending=False)
    months = sorted(mp["month"].unique())
    px = mp.pivot(index="month", columns="symbol", values="close")

    spy = mp[mp["symbol"] == "SPY"].set_index("month")["close"]

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Factor lab: auto-rebalanced fundamentals, top-{TOP_N} — {STAMP}")
    emit()
    emit(f"{len(fund)} symbols with usable point-in-time facts of "
         f"{mp['symbol'].nunique()} in the panels. Long top {DECILE} by each "
         f"rank, equal weight, monthly; {COST_BP_SIDE:.0f}bp/side x measured "
         "turnover. A fundamental enters the rank only after its FILED "
         "date; the universe is each month's own top-500 by dollar volume.")
    emit()

    factors = ("value", "bookval", "quality", "momentum", "combo", "ew500")
    rets: dict[str, list[float]] = {f: [] for f in factors}
    turns: dict[str, list[float]] = {f: [] for f in factors}
    prev_hold: dict[str, set] = {f: set() for f in factors}
    spy_rets: list[float] = []
    used_months: list[str] = []

    for i in range(13, len(months) - 1):
        m, m_next = months[i], months[i + 1]
        uni = mp[(mp["month"] == m) & (mp["rank"] <= TOP_N)]
        date = uni["last_day"].max()
        rows = []
        for sym, close in zip(uni["symbol"], uni["close"]):
            if sym not in px.columns or m_next not in px.index:
                continue
            nxt = px.at[m_next, sym]
            if not np.isfinite(nxt) or not np.isfinite(close) or close <= 0:
                continue
            rec = fund.get(sym, {})
            sh = asof(rec, "shares", date)
            ni = asof(rec, "ni_ttm", date)
            eq = asof(rec, "equity", date)
            mcap = close * sh if sh and sh > 0 else None
            r12 = None
            m12, m1 = months[i - 12], months[i - 1]
            if m12 in px.index and m1 in px.index:
                a, b = px.at[m12, sym], px.at[m1, sym]
                if np.isfinite(a) and np.isfinite(b) and a > 0:
                    r12 = b / a - 1
            rows.append({
                "sym": sym, "fwd": nxt / close - 1,
                "value": (ni / mcap) if (ni is not None and mcap) else np.nan,
                "bookval": (eq / mcap) if (eq is not None and mcap and eq and eq > 0) else np.nan,
                "quality": (ni / eq) if (ni is not None and eq and eq > 0) else np.nan,
                "momentum": r12 if r12 is not None else np.nan,
            })
        f = pd.DataFrame(rows)
        if len(f) < 200:
            continue
        used_months.append(m_next)
        for fac in ("value", "bookval", "quality", "momentum"):
            f[f"rk_{fac}"] = f[fac].rank(pct=True)
        f["rk_combo"] = f[[f"rk_{x}" for x in
                           ("value", "bookval", "quality", "momentum")]].mean(axis=1)
        for fac in factors:
            if fac == "ew500":
                # THE CONTROL. Equal-weight the whole universe: if this
                # matches the combo, the "edge" was the equal-weight tilt
                # against a cap-weighted SPY, not selection. ~8%/mo drift
                # rebalance charged.
                rets[fac].append(f["fwd"].mean() - 0.08 * 2 * COST_BP_SIDE / 1e4)
                turns[fac].append(0.08)
                continue
            ranked = f.dropna(subset=[f"rk_{fac}"]).sort_values(
                f"rk_{fac}", ascending=False)
            hold = ranked.head(DECILE)
            names = set(hold["sym"])
            turn = (1 - len(names & prev_hold[fac]) / DECILE
                    if prev_hold[fac] else 1.0)
            prev_hold[fac] = names
            gross = hold["fwd"].mean()
            rets[fac].append(gross - turn * 2 * COST_BP_SIDE / 1e4)
            turns[fac].append(turn)
        spy_rets.append(spy.get(m_next, np.nan) / spy.get(m, np.nan) - 1)

    emit(f"{len(used_months)} months, {used_months[0]} .. {used_months[-1]}")
    emit()
    emit("| book | ann ret % | ann vol % | Sharpe | maxDD % | avg turn %/mo |")
    emit("|---|---|---|---|---|---|")

    def stats(r: np.ndarray) -> tuple[float, float, float, float]:
        eq = np.cumprod(1 + r)
        years = len(r) / 12
        ann = eq[-1] ** (1 / years) - 1
        vol = r.std(ddof=1) * np.sqrt(12)
        dd = float((1 - eq / np.maximum.accumulate(eq)).max())
        return ann * 100, vol * 100, ann / vol if vol > 0 else 0.0, dd * 100

    for fac in factors:
        r = np.array(rets[fac])
        a, v, s, d = stats(r)
        emit(f"| {fac} (net) | {a:+.1f} | {v:.1f} | {s:.2f} | {d:.1f} "
             f"| {np.mean(turns[fac]) * 100:.0f} |")
    r = np.array(spy_rets)
    r = r[np.isfinite(r)]
    a, v, s, d = stats(r)
    emit(f"| SPY | {a:+.1f} | {v:.1f} | {s:.2f} | {d:.1f} | 0 |")
    emit()
    emit("## Halves (net ann ret % / Sharpe)")
    emit()
    emit("| book | first half | second half |")
    emit("|---|---|---|")
    half = len(used_months) // 2
    for fac in factors + ("SPY",):
        r = np.array(rets[fac]) if fac != "SPY" else np.array(spy_rets)
        r = r[np.isfinite(r)]
        a1, _, s1, _ = stats(r[:half])
        a2, _, s2, _ = stats(r[half:])
        emit(f"| {fac} | {a1:+.1f} / {s1:.2f} | {a2:+.1f} / {s2:.2f} |")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"factor_lab_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "run"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "run": stage_run}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
