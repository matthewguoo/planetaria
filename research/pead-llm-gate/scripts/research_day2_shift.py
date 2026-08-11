"""Can day2_pop share dollars with gff? The entry-shift study.

gff owns 09:28-09:31; day2 enters at the T+2 OPEN. If day2 can enter at the
09:32 close instead without giving up its edge, the two sleeves legally
time-share the same capital in a limited-margin IRA (sale proceeds reuse),
and the Roth book's return moves from the mean of the pair toward the sum.

fetch: T+2 09:29-09:36 minute bars for every taken day2 trade (UP>=5%,
       4-slot selection, same panel as research_mech_account).
score: the account sim entered at the official open vs the 09:32 close.

Run: python scripts/research_day2_shift.py fetch
     python scripts/research_day2_shift.py score
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime

import numpy as np
import pandas as pd

from _paths import BACKEND, CACHE, NOTES, SCRIPTS

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))

from research_common import ET, stamp, write_note  # noqa: E402
from research_mech_account import panel, sessions_and_spy  # noqa: E402
from research_overnight_decomp import tstat  # noqa: E402

SHIFT_F = CACHE / "day2_open_minutes.parquet"
STAMP = stamp()


def taken_trades() -> pd.DataFrame:
    ev = panel()
    up = ev[(ev["move_true"] >= 5.0) & np.isfinite(ev["O2"])
            & np.isfinite(ev["C2"]) & (ev["t2"] != "")].copy()
    up = up.sort_values(["t2", "dv_rank"]).groupby("t2").head(4)
    return up[["symbol", "t2", "O2", "C2", "year"]].rename(columns={"t2": "date"})


def stage_fetch(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    tr = taken_trades()
    prior, have = pd.DataFrame(), set()
    if SHIFT_F.exists():
        prior = pd.read_parquet(SHIFT_F)
        have = set(prior["date"].astype(str))
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    rows = []
    days = [d for d in sorted(tr["date"].unique()) if str(d) not in have]
    print(f"{len(days)} T+2 days to fetch ({len(tr)} trades)")
    for n, day in enumerate(days):
        g = tr[tr["date"] == day]
        d = datetime.fromisoformat(str(day))
        try:
            res = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sorted(set(g["symbol"])),
                timeframe=TimeFrame.Minute,
                start=d.replace(hour=9, minute=29, tzinfo=ET),
                end=d.replace(hour=9, minute=36, tzinfo=ET),
                feed="sip"))
        except Exception as exc:                   # noqa: BLE001
            print(f"  {day}: {str(exc)[:70]}")
            _time.sleep(1.0)
            continue
        for sym in set(g["symbol"]):
            bars = res.data.get(sym) or []
            o930, c932 = None, None
            for b in bars:
                hm = b.timestamp.astimezone(ET).strftime("%H:%M")
                if hm == "09:30" and o930 is None:
                    o930 = float(b.open)
                elif hm == "09:32" and c932 is None:
                    c932 = float(b.close)
            rows.append({"symbol": sym, "date": str(day),
                         "o930": o930, "c932": c932})
        if n % 100 == 0:
            print(f"  {n}/{len(days)} ({day})", flush=True)
        _time.sleep(0.3)
    df = pd.DataFrame(rows)
    allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
    allf = allf.drop_duplicates(subset=["symbol", "date"])
    allf.to_parquet(SHIFT_F)
    print(f"cached {len(allf)} rows -> {SHIFT_F}")


def stage_score(args) -> None:
    tr = taken_trades()
    tr["date"] = tr["date"].astype(str)
    mins = pd.read_parquet(SHIFT_F)
    j = tr.merge(mins, on=["symbol", "date"], how="left")
    j = j.dropna(subset=["o930", "c932"])
    # adjusted C2/O2 vs raw minute prints: shift measured in the minute basis,
    # applied to the adjusted daily return (same-session ratio, basis-safe).
    j["ret_open"] = (j["C2"] / j["O2"] - 1) - 6.0 / 1e4
    j["ret_932"] = (j["C2"] / j["O2"] * (j["o930"] / j["c932"]) - 1) - 6.0 / 1e4

    sessions, spy = sessions_and_spy()
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# day2_pop entered at 09:32 — the stacking study — {STAMP}")
    emit()
    emit(f"{len(j)} of {len(tr)} taken trades have T+2 opening minutes. "
         "Entry at the official open vs the 09:32 close (frees 09:28-09:31 "
         "for gff on the same dollars); exit 15:55 close; net 6bp.")
    emit()
    emit("| entry | n | net bp/tr | t | win% | ann ret % | Sharpe |")
    emit("|---|---|---|---|---|---|---|")
    for name, col in (("official open", "ret_open"), ("09:32 close", "ret_932")):
        s_idx = {d: i for i, d in enumerate(sessions)}
        daily = np.zeros(len(sessions))
        for d, g in j.groupby("date"):
            i = s_idx.get(d)
            if i is not None:
                daily[i] += g[col].sum() / 4
        ser = pd.Series(daily, index=sessions)
        eq = np.cumprod(1 + ser.to_numpy())
        yrs = len(sessions) / 252
        ann = float(eq[-1] ** (1 / yrs) - 1) * 100
        sh = float(ser.mean() / ser.std(ddof=1) * np.sqrt(252))
        r = j[col].to_numpy()
        emit(f"| {name} | {len(r)} | {r.mean() * 1e4:+.1f} | {tstat(r * 1e4):+.2f} "
             f"| {(r > 0).mean() * 100:.0f} | {ann:+.2f} | {sh:+.2f} |")
    d = (j["ret_932"] - j["ret_open"]).to_numpy() * 1e4
    emit()
    emit(f"Shift cost per trade: {d.mean():+.1f}bp (t {tstat(d):+.2f}) — "
         "negative means the first two minutes carried edge the shift "
         "gives up; positive means the open print was adverse.")
    write_note(NOTES / f"day2_shift_{STAMP}.md", lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "score"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    main()
