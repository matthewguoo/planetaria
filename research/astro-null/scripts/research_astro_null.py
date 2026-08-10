"""The planetaria null battery: calendar and astronomical partitions of SPY.

The repo is named planetaria; it was going to happen. The standard is the
same as every other study here: state the published claim precisely, run it
once as pre-registered, report every row including the failures, and correct
for the number of tests. This battery is a CONFIRMATION battery of published
claims — each partition below is the convention of a named paper (or named
folklore), not a searched rule. Bonferroni is applied over the whole table.

Partitions and their sources:
  moon (7d/15d)   Dichev-Janes 2003 (7-day windows centred on new/full);
                  Yuan-Zheng-Zhu JEmpFin 2006 (15-day windows). Claim: new-
                  moon windows beat full-moon windows by 5-8% annualised.
  mercury retro   folklore, no serious venue. Stations computed from JPL
                  DE421 via skyfield (daily 0h UT sampling, station dates
                  accurate to ~1 day).
  pre-FOMC drift  Lucca-Moench JF 2015: the 24h before scheduled FOMC
                  announcements earned ~3.9%/yr 1994-2011. NY Fed follow-ups
                  report it gone post-2015. Daily proxy: the close-to-close
                  return ENDING on announcement day (contains the 2pm print)
                  and the one ending the day before, reported separately.
  FOMC even week  Cieslak-Morse-Vissing-Jorgensen JF 2019: cycle time in
                  trading days since the announcement; even weeks (days 0-4,
                  10-14, 20-24, ...) carry the whole equity premium.
  turn of month   classic ToM = last trading day through the first three.
                  Etula et al RFS 2020 "dash for cash": returns LOW in the
                  3 days before month-end, HIGH in the 3 days after.
  day of week     Monday vs Friday (Birru JFE 2018 for the speculative
                  version; SPY here is the index form).
  OPEX            third-Friday week and the Friday itself (Stivers-Sun).
  halloween       Bouman-Jacobsen AER 2002: Nov-Apr vs May-Oct.
  santa           last 5 trading days of Dec + first 2 of Jan.
  january         January vs rest.
  friday 13th     folklore control with a literature of its own (Kolb-
                  Rodriguez 1987).

Data: SPY daily closes from stooq (spy.us, split/dividend adjusted, 1993->)
cached in cache/; falls back to the pead-llm-gate bench parquet (2016-2026)
if stooq is unreachable. Two windows per partition where data allows:
1993-2026 and 2016-2026 — a claim that only lives in the long window is a
claim about a market that no longer exists.

FOMC dates: scheduled meetings 2016-2026, statement day, from
federalreserve.gov (fomccalendars.htm + fomchistorical{2016..2020}.htm,
fetched 2026-08-09). The 2020-03-15 emergency Sunday action replaces the
cancelled scheduled March 2020 meeting on the Fed's page; announcement days
that are not trading days are skipped for the drift rows and anchor the
cycle at the next session.

Run:  python scripts/research_astro_null.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY / "_vendor"))   # skyfield + deps, study-local

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ROOT = STUDY.parents[1]
BENCH = ROOT / "research" / "pead-llm-gate" / "cache" / "bench_SPY_2016-01-13_2026-08-06.parquet"

ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
N_PERM = 2000
RNG = np.random.default_rng(20260809)

FOMC = [  # scheduled meetings, statement day; sources in module docstring
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15", "2016-07-27",
    "2016-09-21", "2016-11-02", "2016-12-14",
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14", "2017-07-26",
    "2017-09-20", "2017-11-01", "2017-12-13",
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13", "2018-08-01",
    "2018-09-26", "2018-11-08", "2018-12-19",
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19", "2019-07-31",
    "2019-09-18", "2019-10-30", "2019-12-11",
    "2020-01-29", "2020-03-15", "2020-04-29", "2020-06-10", "2020-07-29",
    "2020-09-16", "2020-11-05", "2020-12-16",
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16", "2021-07-28",
    "2021-09-22", "2021-11-03", "2021-12-15",
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27",
    "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26",
    "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-09",
]


# --------------------------------------------------------------------- data

def spy_daily() -> pd.DataFrame:
    """SPY adjusted closes, longest history available, cached."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "spy_stooq.csv"
    if not f.exists():
        import httpx
        for url in ("https://stooq.com/q/d/l/?s=spy.us&i=d&d1=19930101&d2=20261231",
                    "https://stooq.pl/q/d/l/?s=spy.us&i=d",
                    "https://stooq.com/q/d/l/?s=%5espx&i=d"):
            try:
                r = httpx.get(url, timeout=30, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and r.text.startswith("Date"):
                    f.write_text(r.text, encoding="utf-8")
                    break
                print(f"stooq {url[:40]}...: {r.status_code}")
            except Exception as exc:
                print(f"stooq fetch failed ({str(exc)[:60]})")
    if f.exists():
        df = pd.read_csv(f, parse_dates=["Date"])
        df = df.rename(columns={"Date": "dt", "Close": "close"})[["dt", "close"]]
        src = "stooq spy.us (adjusted)"
    else:
        df = pd.read_parquet(BENCH)
        df["dt"] = pd.to_datetime(df["date"])
        df = df[["dt", "close"]]
        src = "pead bench parquet (2016-2026)"
    df = df.sort_values("dt").reset_index(drop=True)
    df["ret"] = df["close"].pct_change()
    df = df.dropna().reset_index(drop=True)
    print(f"SPY: {len(df)} sessions {df['dt'].min().date()} .. "
          f"{df['dt'].max().date()} [{src}]")
    return df


# ----------------------------------------------------------- ephemeris bits

def ephemeris():
    from skyfield import api
    load = api.Loader(str(CACHE), verbose=False)
    ts = load.timescale()
    eph = load("de421.bsp")
    return ts, eph


def moon_events(ts, eph, y0=1992, y1=2027) -> tuple[list[date], list[date]]:
    from skyfield import almanac
    t0, t1 = ts.utc(y0, 1, 1), ts.utc(y1, 1, 1)
    t, phase = almanac.find_discrete(t0, t1, almanac.moon_phases(eph))
    news = [ti.utc_datetime().date() for ti, p in zip(t, phase) if p == 0]
    fulls = [ti.utc_datetime().date() for ti, p in zip(t, phase) if p == 2]
    return news, fulls


def mercury_retro_mask(ts, eph, dts: pd.Series) -> np.ndarray:
    """True on days Mercury's geocentric apparent ecliptic longitude is
    decreasing, sampled daily at 0h UT (station dates +/- ~1 day)."""
    d0 = dts.min().date() - timedelta(days=2)
    d1 = dts.max().date() + timedelta(days=2)
    days = pd.date_range(d0, d1, freq="D")
    t = ts.utc(np.asarray(days.year), np.asarray(days.month),
               np.asarray(days.day))
    earth, mercury = eph["earth"], eph["mercury"]
    lon = earth.at(t).observe(mercury).apparent().ecliptic_latlon()[1].degrees
    lon = np.unwrap(np.deg2rad(lon))
    retro_day = np.diff(lon) < 0                      # day i -> i+1 decreasing
    lookup = {d.date(): bool(r) for d, r in zip(days[:-1], retro_day)}
    return np.array([lookup.get(d.date(), False) for d in dts])


# ------------------------------------------------------------------- stats

def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    return float((a.mean() - b.mean()) / np.sqrt(va + vb))


def perm_p(ret: np.ndarray, mask: np.ndarray) -> float:
    """Two-sided permutation p for the in-out mean difference."""
    obs = abs(ret[mask].mean() - ret[~mask].mean())
    k = int(mask.sum())
    null = np.empty(N_PERM)
    for i in range(N_PERM):
        pm = np.zeros(len(ret), dtype=bool)
        pm[RNG.choice(len(ret), k, replace=False)] = True
        null[i] = abs(ret[pm].mean() - ret[~pm].mean())
    return float((null >= obs).mean())


def near(dts: pd.Series, events: list[date], cal_days: int) -> np.ndarray:
    ev = pd.to_datetime(pd.Series(events)).sort_values().to_numpy()
    x = dts.to_numpy()
    idx = np.searchsorted(ev, x)
    prev = np.where(idx > 0, x - ev[np.clip(idx - 1, 0, len(ev) - 1)],
                    np.timedelta64(10**9, "s"))
    nxt = np.where(idx < len(ev), ev[np.clip(idx, 0, len(ev) - 1)] - x,
                   np.timedelta64(10**9, "s"))
    best = np.minimum(np.abs(prev), np.abs(nxt))
    return best <= np.timedelta64(cal_days, "D")


def main() -> None:
    spy = spy_daily()
    dts, ret = spy["dt"], spy["ret"].to_numpy()

    ts, eph = ephemeris()
    news, fulls = moon_events(ts, eph)
    retro = mercury_retro_mask(ts, eph, dts)

    fomc_dt = pd.to_datetime(pd.Series(sorted(FOMC)))
    trading = set(dts.dt.date)
    fomc_days = np.isin(dts.dt.date.to_numpy(),
                        [d.date() for d in fomc_dt if d.date() in trading])
    pre_fomc = np.roll(fomc_days, -1)                 # session BEFORE the print
    pre_fomc[-1] = False
    # CMVJ cycle time: trading days since the most recent announcement
    # (announcements on non-sessions anchor at the next session).
    ann_pos = []
    date_index = {d: i for i, d in enumerate(dts.dt.date)}
    for d in fomc_dt:
        dd = d.date()
        while dd not in date_index and dd <= dts.iloc[-1].date():
            dd = dd + timedelta(days=1)
        if dd in date_index:
            ann_pos.append(date_index[dd])
    days_since = np.full(len(dts), 10**6)
    for pos in ann_pos:
        upto = np.arange(len(dts)) - pos
        sel = upto >= 0
        days_since[sel] = np.minimum(days_since[sel], upto[sel])
    cmvj_valid = days_since < 10**5
    even_week = ((days_since // 5) % 2 == 0) & cmvj_valid

    m, dwk, mon = dts.dt.month.to_numpy(), dts.dt.dayofweek.to_numpy(), dts.dt.day.to_numpy()
    # turn of month: last session through first three
    eom = np.zeros(len(dts), bool)
    month_key = dts.dt.to_period("M").astype(str).to_numpy()
    last_of_month = np.r_[month_key[1:] != month_key[:-1], True]
    pos_in_month = np.zeros(len(dts), int)
    cnt = 0
    for i in range(len(dts)):
        cnt = 1 if i == 0 or month_key[i] != month_key[i - 1] else cnt + 1
        pos_in_month[i] = cnt
    tom = last_of_month | (pos_in_month <= 3)
    pre_eom3 = np.zeros(len(dts), bool)              # Etula: 3 days pre month-end
    idx_last = np.where(last_of_month)[0]
    for i in idx_last:
        pre_eom3[max(0, i - 3):i] = True
    post_eom3 = pos_in_month <= 3
    # OPEX: third Friday and its week
    is_friday = dwk == 4
    third_friday = is_friday & (mon >= 15) & (mon <= 21)
    opex_week = np.zeros(len(dts), bool)
    wk = dts.dt.isocalendar().week.to_numpy()
    yr = dts.dt.isocalendar().year.to_numpy()
    opex_keys = set(zip(yr[third_friday], wk[third_friday]))
    for i, key in enumerate(zip(yr, wk)):
        opex_week[i] = key in opex_keys
    halloween = np.isin(m, [11, 12, 1, 2, 3, 4])
    january = m == 1
    santa = np.zeros(len(dts), bool)
    for year in range(dts.iloc[0].year, dts.iloc[-1].year + 1):
        dec = np.where((dts.dt.year == year) & (m == 12))[0]
        jan = np.where((dts.dt.year == year + 1) & (m == 1))[0]
        if len(dec) >= 5:
            santa[dec[-5:]] = True
        if len(jan) >= 2:
            santa[jan[:2]] = True
    fri13 = is_friday & (mon == 13)

    partitions: list[tuple[str, np.ndarray, np.ndarray | None]] = [
        ("new moon +/-3d vs full +/-3d (Dichev-Janes 7d)",
         near(dts, news, 3), near(dts, fulls, 3)),
        ("new moon +/-7d vs full +/-7d (Yuan-Zheng-Zhu 15d)",
         near(dts, news, 7), near(dts, fulls, 7)),
        ("Mercury retrograde vs direct", retro, None),
        ("FOMC announcement day (2016+)", fomc_days, None),
        ("pre-FOMC session (Lucca-Moench proxy, 2016+)", pre_fomc, None),
        ("FOMC even week (CMVJ, 2016+)", even_week & cmvj_valid, None),
        ("turn of month (last+3)", tom, None),
        ("3 days BEFORE month-end (Etula low)", pre_eom3, None),
        ("first 3 days of month (Etula high)", post_eom3, None),
        ("Monday", dwk == 0, None),
        ("Friday", is_friday, None),
        ("OPEX week", opex_week, None),
        ("OPEX Friday", third_friday, None),
        ("Nov-Apr (Halloween)", halloween, None),
        ("January", january, None),
        ("Santa (last 5 Dec + first 2 Jan)", santa, None),
        ("Friday the 13th", fri13, None),
    ]

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    n_tests = len(partitions) * 2
    emit(f"# planetaria null battery — {STAMP}")
    emit()
    emit(f"SPY daily total returns, {dts.iloc[0].date()} .. "
         f"{dts.iloc[-1].date()}. Each partition is a published convention "
         "(sources in the script docstring); the battery ran once, every row "
         f"is shown. {n_tests} tests -> Bonferroni bar at p < "
         f"{0.05 / n_tests:.4f}, i.e. |t| >~ "
         f"{3.1:.1f}. bp/d = mean daily return in-partition; edge = in "
         "minus out (or minus the paired window where one is named).")
    emit()

    for window_name, lo in (("1993-2026 (full)", None),
                            ("2016-2026", pd.Timestamp("2016-01-01"))):
        sel = np.ones(len(dts), bool) if lo is None else (dts >= lo).to_numpy()
        r = ret[sel]
        span = f"{dts[sel].min().date()}..{dts[sel].max().date()}"
        emit(f"## {window_name} [{span}] — {int(sel.sum())} sessions, "
             f"mean {r.mean() * 1e4:+.1f}bp/d")
        emit()
        emit("| partition | n_in | in bp/d | out bp/d | edge bp/d | t | perm p | ann edge % |")
        emit("|---|---|---|---|---|---|---|---|")
        for name, mask, pair in partitions:
            if "2016+" in name and lo is None:
                continue
            a = ret[sel & mask]
            b = ret[sel & pair] if pair is not None else ret[sel & ~mask]
            if len(a) < 8 or len(b) < 8:
                continue
            edge = (a.mean() - b.mean()) * 1e4
            t = welch_t(a, b)
            pp = perm_p(r, mask[sel]) if pair is None else float("nan")
            pcol = f"{pp:.3f}" if np.isfinite(pp) else "paired"
            emit(f"| {name} | {len(a)} | {a.mean() * 1e4:+.1f} "
                 f"| {b.mean() * 1e4:+.1f} | {edge:+.1f} | {t:+.2f} "
                 f"| {pcol} | {edge * 252 / 100:+.1f} |")
        emit()

    emit("Reading guide: with ~34 tests, two or three |t|>2 rows are the "
         "EXPECTED yield under a global null. Only rows past the Bonferroni "
         "bar in BOTH windows deserve a second look, and any survivor gets "
         "a pre-registered forward test before a dollar moves.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"astro_null_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
