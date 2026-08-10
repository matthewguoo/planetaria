"""gap_fail_fade extended to a decade: 2016-2021 joins the cached 2022-2026.

Same universe rule as research_open_window.gap_events (dv_prev >= $150M,
c_prev >= $10, |gap| >= 1.5%, top-8/day by |gap|*log(dv)), same premarket
convention as stage_fetch_pm, same registered account sim as
research_gff_account — nothing re-chosen. The only new ingredient is six
more years of premarket + auction minutes, one SIP request per day
(08:59-09:36 covers the 09:15/09:29 marks, the auction print, and the
09:31 exit in one call). Days where the 09:31 bar never printed (halts,
e.g. 2020-03-16) drop out exactly as missing marks always have — live,
those are the days the hard 09:31:30 time stop meets a halted book, which
the pre-registration's stop rules would surface.

Run: python scripts/research_gff_decade.py fetch    (~1,500 days, resumable)
     python scripts/research_gff_decade.py score
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(STUDY / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
DECADE_F = CACHE / "gff_decade_2016_2021.parquet"

MIN_TURN_BP = 20.0
MIN_PRICE = 10.0
SLOTS_PER_LEG = 2
POSITION_FRAC = 0.25
COSTS_BP = (6.0, 10.0, 15.0, 20.0)
ALLOC = 10_000.0


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def gap_events_decade() -> pd.DataFrame:
    """research_open_window.gap_events verbatim, window 2016-2021."""
    frames = [pd.read_parquet(p, columns=["symbol", "date", "o", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = (df.drop_duplicates(subset=["symbol", "date"])
          .sort_values(["symbol", "date"]))
    g = df.groupby("symbol", sort=False)
    df["c_prev"] = g["c"].shift(1)
    df["dv_prev"] = df["c_prev"] * g["v"].shift(1)
    df["dt"] = pd.to_datetime(df["date"])
    df["gapd"] = (df["dt"] - g["dt"].shift(1)).dt.days
    df["gap"] = df["o"] / df["c_prev"] - 1
    ok = ((df["date"] >= "2016-01-01") & (df["date"] <= "2021-12-31")
          & (df["gapd"] <= 5) & (df["dv_prev"] >= 1.5e8)
          & (df["c_prev"] >= 10) & (df["gap"].abs() >= 0.015))
    ev = df[ok].copy()
    ev["score"] = ev["gap"].abs() * np.log(ev["dv_prev"])
    ev["rank"] = ev.groupby("date")["score"].rank(ascending=False)
    return ev[ev["rank"] <= 8][["symbol", "date", "gap", "dv_prev"]]


def stage_fetch(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    ev = gap_events_decade()
    prior, have = pd.DataFrame(), set()
    if DECADE_F.exists():
        prior = pd.read_parquet(DECADE_F)
        have = set(prior["date"].astype(str))
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    rows = []
    days = [d for d in sorted(ev["date"].unique()) if d not in have]
    print(f"{len(days)} decade days to fetch ({len(ev)} events)")
    for n, day in enumerate(days):
        g = ev[ev["date"] == day]
        d = datetime.fromisoformat(day)
        try:
            res = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sorted(g["symbol"]),
                timeframe=TimeFrame.Minute,
                start=d.replace(hour=8, minute=59, tzinfo=ET),
                end=d.replace(hour=9, minute=36, tzinfo=ET),
                feed="sip"))
        except Exception as exc:                   # noqa: BLE001
            print(f"  {day}: {str(exc)[:70]}")
            _time.sleep(1.0)
            continue
        for _, e in g.iterrows():
            bars = res.data.get(e["symbol"]) or []
            marks, vol, last = {}, 0.0, None
            open_px, c1 = None, None
            for b in bars:
                hm = b.timestamp.astimezone(ET).strftime("%H:%M")
                if hm < "09:30":
                    last = float(b.close)
                    vol += float(b.volume)
                    if hm in ("09:00", "09:15"):
                        marks.setdefault(hm, float(b.close))
                elif hm == "09:30" and open_px is None:
                    open_px = float(b.open)
                elif hm == "09:31" and c1 is None:
                    c1 = float(b.close)
            if last is None:
                continue
            rows.append({"symbol": e["symbol"], "date": day,
                         "gap": float(e["gap"]),
                         "pm0915": marks.get("09:15"), "pm0929": last,
                         "pmvol": vol, "open": open_px, "c1": c1})
        if n % 100 == 0:
            print(f"  {n}/{len(days)} ({day}, {len(rows)} rows)", flush=True)
        _time.sleep(0.3)
    df = pd.DataFrame(rows)
    allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
    allf = allf.drop_duplicates(subset=["symbol", "date"])
    allf.to_parquet(DECADE_F)
    print(f"cached {len(allf)} decade rows -> {DECADE_F}")


def load_union() -> pd.DataFrame:
    """2016-2021 decade rows + the 2022-2026 study caches, one frame."""
    dec = pd.read_parquet(DECADE_F)
    paths = pd.read_parquet(CACHE / "open_paths.parquet").drop_duplicates(
        subset=["symbol", "date"])
    pm = pd.read_parquet(CACHE / "pm_marks.parquet").drop_duplicates(
        subset=["symbol", "date"])
    j = paths.merge(pm, on=["symbol", "date"], how="inner")
    j["c1"] = [np.asarray(v, float)[1] for v in j["c"]]
    new = j[["symbol", "date", "gap", "pm0915", "pm0929", "pmvol",
             "open", "c1"]]
    out = pd.concat([dec, new], ignore_index=True).drop_duplicates(
        subset=["symbol", "date"]).sort_values("date")
    return out


def spy_daily_ret() -> pd.Series:
    frames = [pd.read_parquet(f) for f in sorted(PEAD_CACHE.glob("bench_SPY_*.parquet"))]
    spy = (pd.concat(frames, ignore_index=True)
           .drop_duplicates("date").sort_values("date"))
    spy["date"] = spy["date"].astype(str)
    return spy.set_index("date")["close"].pct_change().rename("SPY")


def stage_score(args) -> None:
    df = load_union()
    df = df.dropna(subset=["pm0915", "pm0929", "open", "c1"])
    df = df[df["open"] >= MIN_PRICE]
    pm_trend = (df["pm0929"] / df["pm0915"] - 1).to_numpy() * 1e4
    gap = df["gap"].to_numpy()
    fading = (np.abs(pm_trend) >= MIN_TURN_BP) & \
             (np.sign(pm_trend) != np.sign(gap * 1e4))
    ret_bp = -np.sign(gap) * (df["c1"].to_numpy() / df["open"].to_numpy() - 1) * 1e4
    ev = pd.DataFrame({
        "date": df["date"].to_numpy(), "symbol": df["symbol"].to_numpy(),
        "bp": ret_bp, "long": gap < 0, "pmvol": df["pmvol"].to_numpy(),
    })[fading].dropna(subset=["bp"])

    spy = spy_daily_ret()
    all_days = [d for d in spy.index if "2016-01-01" <= d <= max(ev["date"])]

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# gap_fail_fade, the decade, registered config — {STAMP}")
    emit()
    emit(f"{len(ev):,} qualifying fades on {ev['date'].nunique():,} days, "
         "2016-01..2026-08. Same universe rule, same premarket convention, "
         "same slots (2/leg by premarket volume, 25% each), same entry "
         "(auction print) and exit (09:31 close) as the registered config. "
         "Flat days included.")
    emit()
    emit("| legs | cost bp | trades | net bp/tr | t | win% | ann ret % "
         "| Sharpe | maxDD % | alpha %/yr | beta |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    daily_out = {}
    for legs_name, mask in (("LONG only", ev["long"]),
                            ("both legs", pd.Series(True, index=ev.index))):
        sel = (ev[mask]
               .sort_values(["date", "pmvol"], ascending=[True, False])
               .groupby(["date", "long"]).head(SLOTS_PER_LEG))
        for cost in COSTS_BP:
            net = sel["bp"] - cost
            day_ret = (net * POSITION_FRAC / 1e4).groupby(sel["date"]).sum()
            ret = day_ret.reindex(all_days).fillna(0.0)
            eq = np.cumprod(1 + ret.to_numpy())
            years = len(all_days) / 252
            ann = float(eq[-1] ** (1 / years) - 1) * 100
            sharpe = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252))
            mdd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
            j = pd.concat([ret, spy], axis=1, join="inner").dropna()
            y, x = j.iloc[:, 0].to_numpy(), j.iloc[:, 1].to_numpy()
            beta = float(np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1))
            alpha = float((y.mean() - beta * x.mean()) * 252 * 100)
            emit(f"| {legs_name} | {cost:.0f} | {len(sel):,} "
                 f"| {net.mean():+.1f} | {tstat(net.to_numpy()):+.2f} "
                 f"| {(net > 0).mean() * 100:.0f} | {ann:+.2f} "
                 f"| {sharpe:+.2f} | {mdd:.1f} | {alpha:+.2f} | {beta:+.3f} |")
            if cost == 10.0:
                daily_out[legs_name] = ret
    emit()
    emit("## By year, net@10 bp/trade")
    emit()
    emit("| year | LONG bp/tr | t | n | BOTH bp/tr | t | n |")
    emit("|---|---|---|---|---|---|---|")
    sel_l = (ev[ev["long"]].sort_values(["date", "pmvol"], ascending=[True, False])
             .groupby("date").head(SLOTS_PER_LEG))
    sel_b = (ev.sort_values(["date", "pmvol"], ascending=[True, False])
             .groupby(["date", "long"]).head(SLOTS_PER_LEG))
    for y in sorted(set(d[:4] for d in ev["date"])):
        L = sel_l[sel_l["date"].str.startswith(y)]["bp"] - 10.0
        B = sel_b[sel_b["date"].str.startswith(y)]["bp"] - 10.0
        emit(f"| {y} | {L.mean():+.1f} | {tstat(L.to_numpy()):+.2f} | {len(L)} "
             f"| {B.mean():+.1f} | {tstat(B.to_numpy()):+.2f} | {len(B)} |")

    for name, key in (("LONG only", "gff_long"), ("both legs", "gff_both")):
        r = daily_out[name]
        pd.DataFrame({"d": r.index, "ret": r.to_numpy()}).to_parquet(
            CACHE / f"account_daily_{key}_decade.parquet")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_gff_decade.py score` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gff_decade_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "score"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    main()
