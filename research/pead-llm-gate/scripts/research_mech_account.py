"""Account-level backtests for the two LLM-free PEAD sleeves. No sweeps.

Reuses research_overnight_decomp's loaders so the panels are byte-identical
to the day2/mech-carry notes; adds what those notes stopped short of: the
account daily-return series, alpha/beta against SPY, and by-year rows, at
the slices the notes themselves chose (day2_pop: UP >= 5%, 4 slots, net
6bp; mech carry: UP >= 2%, top-5/night, net 23.2bp measured round trip,
5 equal-weight slots). Selection honesty: both slices were picked after
looking at their own grids — the by-year rows are the check.

Run: python scripts/research_mech_account.py
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from _paths import CACHE, NOTES, SCRIPTS

sys.path.insert(0, str(SCRIPTS))

from research_common import stamp, write_note  # noqa: E402
from research_overnight_decomp import (  # noqa: E402
    MIN_DV,
    MIN_PRICE,
    attach_legs,
    load_ohlc,
    load_raw,
    tstat,
)

STAMP = stamp()


def sessions_and_spy() -> tuple[list[str], pd.Series]:
    spy = pd.read_parquet(CACHE / "bench_SPY_2016-01-13_2026-08-06.parquet")
    spy["date"] = spy["date"].astype(str)
    spy = spy.sort_values("date")
    ret = spy.set_index("date")["close"].pct_change().rename("SPY")
    return list(spy["date"]), ret


def alpha_beta(ret: pd.Series, bench: pd.Series) -> tuple[float, float]:
    j = pd.concat([ret, bench], axis=1, join="inner").dropna()
    if len(j) < 60:
        return float("nan"), float("nan")
    y, x = j.iloc[:, 0].to_numpy(), j.iloc[:, 1].to_numpy()
    beta = float(np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1))
    alpha = float((y.mean() - beta * x.mean()) * 252 * 100)
    return alpha, beta


def panel() -> pd.DataFrame:
    ohlc = load_ohlc()
    raw = load_raw()
    evs = [pd.read_parquet(p) for p in sorted(CACHE.glob("events_v2_*.parquet"))]
    ev = pd.concat(evs, ignore_index=True).drop_duplicates(subset=["symbol", "date"])
    ev = attach_legs(ev, ohlc)
    ct_raw = np.full(len(ev), np.nan)
    t1_date = np.full(len(ev), "", dtype=object)
    t2_date = np.full(len(ev), "", dtype=object)
    sym = ev["symbol"].to_numpy()
    dat = ev["date"].astype(str).to_numpy()
    for i in range(len(ev)):
        got = raw.get(sym[i])
        if got is None:
            continue
        dates, _o, c = got
        j = np.searchsorted(dates, dat[i], side="right")
        if j - 1 >= 0 and dates[j - 1] == dat[i]:
            ct_raw[i] = c[j - 1]
        if j < len(dates):
            t1_date[i] = dates[j]
        if j + 1 < len(dates):
            t2_date[i] = dates[j + 1]
    ev["ct_raw"] = ct_raw
    ev["t1"], ev["t2"] = t1_date, t2_date
    ev = ev[np.isfinite(ev["ct_raw"]) & (ev["dv_prior"] >= MIN_DV)
            & (ev["ct_raw"] >= MIN_PRICE)].reset_index(drop=True)
    ev["move_true"] = (ev["react"] / ev["ct_raw"] - 1) * 100
    ev["dv_rank"] = ev.groupby("date")["dv_prior"].rank(ascending=False)
    ev["year"] = ev["date"].astype(str).str[:4]
    return ev


def account_rows(name, daily: pd.Series, sessions, spy, emit,
                 trades: pd.Series, by_year_key: pd.Series):
    eq = np.cumprod(1 + daily.to_numpy())
    years = len(sessions) / 252
    ann = float(eq[-1] ** (1 / years) - 1) * 100
    vol = float(daily.std(ddof=1) * np.sqrt(252) * 100)
    sharpe = float(daily.mean() / daily.std(ddof=1) * np.sqrt(252))
    mdd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
    a, b = alpha_beta(daily, spy)
    emit(f"| {name} | {len(trades)} | {trades.mean() * 1e4:+.1f} "
         f"| {tstat(trades.to_numpy() * 1e4):+.2f} "
         f"| {(trades > 0).mean() * 100:.0f} | {ann:+.2f} | {vol:.1f} "
         f"| {sharpe:+.2f} | {mdd:.1f} | {a:+.2f} | {b:+.3f} |")
    emit_rows = []
    for y in sorted(set(by_year_key)):
        x = trades[by_year_key == y].to_numpy() * 1e4
        if len(x) >= 20:
            emit_rows.append(f"| {y} | {len(x)} | {x.mean():+.1f} | {tstat(x):+.2f} |")
    return emit_rows


def main() -> None:
    ev = panel()
    sessions, spy = sessions_and_spy()
    s_index = {d: i for i, d in enumerate(sessions)}
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Mechanical PEAD sleeves, account level — {STAMP}")
    emit()
    emit(f"{len(ev)} clean-anchor AMC events 2016-2026, ${MIN_DV / 1e6:.0f}M "
         "floor. Flat days included in every daily series; alpha/beta are "
         "daily OLS vs SPY, annualized.")
    emit()
    emit("| sleeve | trades | net bp/tr | t | win% | ann ret % | ann vol % "
         "| Sharpe | maxDD % | alpha %/yr | beta |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|")

    # day2_pop: UP >= 5%, long O2->C2, 4 slots, net 6bp.
    up = ev[(ev["move_true"] >= 5.0) & np.isfinite(ev["O2"])
            & np.isfinite(ev["C2"]) & (ev["t2"] != "")].copy()
    up["ret"] = (up["C2"] / up["O2"] - 1) - 6.0 / 1e4
    up = up.sort_values(["t2", "dv_rank"])
    daily = np.zeros(len(sessions))
    taken_rows = []
    for d, g in up.groupby("t2"):
        i = s_index.get(str(d))
        if i is None:
            continue
        g = g.head(4)
        taken_rows.append(g)
        daily[i] += g["ret"].sum() / 4
    taken = pd.concat(taken_rows)
    d2_daily = pd.Series(daily, index=sessions, name="day2")
    yr_rows = account_rows("day2_pop (UP>=5%, 4 slots, net 6bp)", d2_daily,
                           sessions, spy, emit, taken["ret"], taken["year"])
    d2_year_rows = yr_rows

    # mech carry long: UP >= 2%, top-5/night, react -> next open, net 23.2bp.
    car = ev[(ev["move_true"] >= 2.0) & (ev["dv_rank"] <= 5)
             & np.isfinite(ev["O1"]) & np.isfinite(ev["CT"])
             & (ev["t1"] != "")].copy()
    onemove = 1 + car["move_true"] / 100
    car["ret"] = ((car["O1"] / car["CT"]) / onemove - 1) - 23.2 / 1e4
    car = car.sort_values(["t1", "dv_rank"])
    daily = np.zeros(len(sessions))
    taken_rows = []
    for d, g in car.groupby("t1"):
        i = s_index.get(str(d))
        if i is None:
            continue
        g = g.head(5)
        taken_rows.append(g)
        daily[i] += g["ret"].sum() / 5
    taken_c = pd.concat(taken_rows)
    mc_daily = pd.Series(daily, index=sessions, name="mech_carry")
    mc_year_rows = account_rows(
        "mech_carry long (UP>=2%, top-5, net 23.2bp)", mc_daily,
        sessions, spy, emit, taken_c["ret"], taken_c["year"])
    emit()

    emit("## By year, net bp/trade")
    emit()
    emit("| year | day2 n | bp | t |   | year | carry n | bp | t |")
    emit("|---|---|---|---|---|---|---|---|---|")
    for a_row, b_row in zip(d2_year_rows, mc_year_rows):
        emit(a_row + " " + b_row[1:])
    emit()
    emit("day2_pop rides only the UP side (the DOWN side is flat and needs "
         "shorts); mech carry is the flagship's overnight leg with no LLM "
         "and no AH fill — but also no gate, and its year rows swing hard.")

    pd.DataFrame({"d": d2_daily.index, "ret": d2_daily.to_numpy()}).to_parquet(
        CACHE / "account_daily_day2.parquet")
    pd.DataFrame({"d": mc_daily.index, "ret": mc_daily.to_numpy()}).to_parquet(
        CACHE / "account_daily_mech_carry.parquet")

    write_note(NOTES / f"mech_account_{STAMP}.md", lines)


if __name__ == "__main__":
    main()
