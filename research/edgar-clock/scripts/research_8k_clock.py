"""The evening 8-K clock, sized as a go/no-go — queue #4 (Chan-Marsh).

The fund's event inventory is earnings 8-Ks. Chan-Marsh's claim covers
the BROADER 8-K flow (M&A, guidance, officer changes, Reg FD). Before
anyone builds acceptance-time plumbing, this measures the crude version:
ALL 8-Ks by FILING DATE from the EDGAR daily indexes, joined to the
daily panel, EXCLUDING known earnings events — the drift of the new
inventory at daily granularity. If nothing shows here, the fine clock is
not worth building; if something does, the acceptance-time study (the
real Chan-Marsh replication) gets funded.

Design, pre-stated:
  events   8-K / 8-K/A rows from daily form indexes 2022-2026, mapped
           CIK -> symbol via the factor lab's point-in-time facts;
           liquidity floor $150M prior-day dollar volume, px >= $5;
           symbol-dates within 1 day of an events_v2 earnings event are
           EXCLUDED (that flow is already owned).
  drift    from the filing date D (daily granularity, stated blur):
           close(D) -> open(D+1); open(D+1) -> close(D+1);
           close(D) -> close(D+3). Long-only readout AND signed-by-
           nothing (unconditional) — this is inventory scouting, not a
           strategy; no direction model is fitted.
  verdict  go (build the acceptance-time clock) if any window shows
           |drift| with t >= 2.5 and >= ~10bp on the non-earnings flow,
           year-stable-ish; else the queue item closes.

Run:  python scripts/research_8k_clock.py fetch    (~1,150 index files)
      python scripts/research_8k_clock.py score
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
FL_CACHE = ROOT / "research" / "factor-lab" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
IDX_F = CACHE / "form8k_2022_2026.parquet"

START, END = date(2022, 1, 3), date(2026, 8, 8)
UA = "planetaria-research matthewguo.x86@gmail.com"
DV_MIN, PX_MIN = 1.5e8, 5.0


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def stage_fetch(args) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    prior, have = pd.DataFrame(), set()
    if IDX_F.exists():
        prior = pd.read_parquet(IDX_F)
        have = set(prior["date"].astype(str))
    rows = []
    d = START
    n_done = 0
    with httpx.Client(headers={"User-Agent": UA}, timeout=20) as http:
        while d <= END:
            if d.weekday() >= 5 or d.isoformat() in have:
                d += timedelta(days=1)
                continue
            q = (d.month - 1) // 3 + 1
            url = (f"https://www.sec.gov/Archives/edgar/daily-index/"
                   f"{d.year}/QTR{q}/form.{d.strftime('%Y%m%d')}.idx")
            try:
                r = http.get(url)
                if r.status_code == 403:
                    print(f"  403 at {d} — slowing down")
                    _time.sleep(10)
                    continue
                if r.status_code != 200:
                    d += timedelta(days=1)      # holiday: no index file
                    continue
                for line in r.text.splitlines():
                    if not line.startswith("8-K"):
                        continue
                    parts = line.split()
                    # FORM TYPE | Company Name... | CIK | Date | file
                    if len(parts) < 5:
                        continue
                    form = parts[0]
                    if form not in ("8-K", "8-K/A"):
                        continue
                    cik = parts[-3]
                    if not cik.isdigit():
                        continue
                    rows.append({"date": d.isoformat(), "cik": int(cik),
                                 "form": form})
                n_done += 1
                if n_done % 50 == 0:
                    print(f"  {n_done} index days ({d}, {len(rows):,} 8-K "
                          "rows)", flush=True)
            except Exception as exc:               # noqa: BLE001
                print(f"  {d}: {str(exc)[:60]}")
                _time.sleep(2)
                continue
            _time.sleep(0.15)
            d += timedelta(days=1)
    df = pd.DataFrame(rows)
    allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
    allf = allf.drop_duplicates()
    allf.to_parquet(IDX_F)
    print(f"cached {len(allf):,} 8-K filings -> {IDX_F}")


def stage_score(args) -> None:
    idx = pd.read_parquet(IDX_F)
    facts = pd.read_parquet(FL_CACHE / "slim_facts.parquet",
                            columns=["cik", "symbol"])
    cmap = facts.drop_duplicates().groupby("cik")["symbol"].first()
    idx["symbol"] = idx["cik"].map(cmap)
    ev = idx.dropna(subset=["symbol"]).drop_duplicates(
        subset=["symbol", "date"])

    frames = [pd.read_parquet(p, columns=["symbol", "date", "o", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))]
    px = pd.concat(frames, ignore_index=True)
    px["date"] = px["date"].astype(str)
    px = (px[px["date"] >= "2021-11-01"]
          .drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"]))
    g = px.groupby("symbol", sort=False)
    px["dv_prev"] = (px["c"] * px["v"]).groupby(px["symbol"]).shift(1)
    px["o1"] = g["o"].shift(-1)
    px["c1"] = g["c"].shift(-1)
    px["c3"] = g["c"].shift(-3)

    j = ev.merge(px, on=["symbol", "date"], how="inner")
    j = j[(j["dv_prev"] >= DV_MIN) & (j["c"] >= PX_MIN)]

    earn = pd.concat([pd.read_parquet(f, columns=["symbol", "date"])
                      for f in sorted(PEAD_CACHE.glob("events_v2_*.parquet"))],
                     ignore_index=True)
    earn["date"] = earn["date"].astype(str)
    bad = set()
    for s, d in zip(earn["symbol"], earn["date"]):
        dd = pd.Timestamp(d)
        for k in (-1, 0, 1):
            bad.add((s, (dd + pd.Timedelta(days=k)).strftime("%Y-%m-%d")))
    is_earn = [ (s, d) in bad for s, d in zip(j["symbol"], j["date"]) ]
    j["earnings_adjacent"] = is_earn
    j["year"] = j["date"].str[:4]
    j["on_bp"] = (j["o1"] / j["c"] - 1) * 1e4
    j["d1_bp"] = (j["c1"] / j["o1"] - 1) * 1e4
    j["d3_bp"] = (j["c3"] / j["c"] - 1) * 1e4

    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    emit(f"# The 8-K clock, go/no-go — {STAMP}")
    emit()
    emit(f"{len(j):,} liquid 8-K filing-days 2022-26 "
         f"({int(j['earnings_adjacent'].sum()):,} earnings-adjacent, "
         "excluded from the headline). Daily-granularity blur stated: the "
         "filing date mixes pre-open, intraday and evening acceptances.")
    emit()
    emit("| flow | window | n | bp | t |")
    emit("|---|---|---|---|---|")
    clean = j[~j["earnings_adjacent"]]
    dirty = j[j["earnings_adjacent"]]
    for label, frame in (("NON-earnings 8-K", clean),
                         ("earnings-adjacent (control)", dirty)):
        for wl, col in (("close(D)->open(D+1)", "on_bp"),
                        ("open(D+1)->close(D+1)", "d1_bp"),
                        ("close(D)->close(D+3)", "d3_bp")):
            x = frame[col].dropna().to_numpy()
            emit(f"| {label} | {wl} | {len(x):,} | {np.mean(x):+.1f} "
                 f"| {tstat(x):+.2f} |")
    emit()
    emit("| year (non-earnings, close->open) | n | bp | t |")
    emit("|---|---|---|---|")
    for y in sorted(clean["year"].unique()):
        x = clean.loc[clean["year"] == y, "on_bp"].dropna().to_numpy()
        emit(f"| {y} | {len(x):,} | {np.mean(x):+.1f} | {tstat(x):+.2f} |")
    emit()
    emit("Verdict rule (docstring): GO if any non-earnings window shows "
         ">= ~10bp at t >= 2.5 year-stable-ish; else the queue item "
         "closes at daily granularity.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_8k_clock.py score` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"clock8k_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "score"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
