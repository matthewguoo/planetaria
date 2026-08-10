"""Is the 0DTE variance premium still paid — and to whom?

The ingredient study before any structure sweep. Every 0DTE strategy —
condors, flies, covered anything — is a position on one number: the gap
between what the market charges for same-day movement and what movement is
delivered. Measure the number first. If selling the 10:00 ATM straddle to
expiry has no edge gross of costs, no defined-risk decoration will save it;
if it has, the sweep can start from a measured base rate rather than a
backtest fantasy.

Design: on every session since Alpaca's options history begins (2024-02),
price the SPY and QQQ same-day-expiry ATM straddle at ~10:00 ET, and settle
it against |close - strike| at 16:00. Also mark it at 14:00 and 15:30 so
the theta path is visible (who pays for the morning, who pays for the
afternoon). Strike = underlying at 10:00 rounded to the nearest dollar.

Prices are OPRA minute-bar closes for each leg (trade prints, mid-ish for
the most liquid contracts on the exchange; ATM same-day SPY prints every
minute). Entry uses the first leg print in [10:00, 10:10]; days where
either leg has no print in the window are dropped and counted. Payoff is
expiry intrinsic — early exercise and pin friction ignored, which flatters
the SELLER by a hair on the worst pin days.

BASIS RULE (learned 2026-08-10 the hard way): strikes and payoffs are raw
dollars, so the underlying here is fetched RAW — never the Adjustment.ALL
bars the other studies use. An adjusted close against a raw strike shifts
every payoff by the cumulative dividend factor.

Stages:
    under   raw 30-minute underlying bars 2024-01 -> now (SPY, QQQ)
    probe   one known session end-to-end, prints what the API returns
    fetch   every session x {SPY, QQQ} x {C, P} minute bars -> cache
    score   the note: seller edge, implied vs realized, tails, timing
    wings   QQQ wing legs (K +/- w) at 0.3/0.6/1.0% widths -> cache
    flies   the defined-risk sweep: iron flies at each entry x width,
            which is the form the account can actually hold (naked short
            calls are broker-refused; short puts are cash-secured)

Run:  python scripts/research_0dte_straddle.py under
      python scripts/research_0dte_straddle.py probe
      python scripts/research_0dte_straddle.py fetch
      python scripts/research_0dte_straddle.py score
      python scripts/research_0dte_straddle.py wings
      python scripts/research_0dte_straddle.py flies
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime, timedelta
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

START = datetime(2024, 2, 1, tzinfo=ET)      # options history begins ~2024-02
UNDERLYINGS = ("SPY", "QQQ")
MARKS = ("10:00", "14:00", "15:30")
ENTRY_GRACE_MIN = 10


def _settings():
    from app.config import get_settings
    return get_settings()


def occ(root: str, d, right: str, strike: float) -> str:
    return f"{root}{d.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"


# ------------------------------------------------------------------- under

def stage_under(args) -> None:
    """RAW 30-minute bars for the underlyings — strike and payoff basis."""
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    CACHE.mkdir(parents=True, exist_ok=True)
    s = _settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    for sym in UNDERLYINGS:
        out = api.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(30, TimeFrameUnit.Minute),
            start=datetime(2024, 1, 1, tzinfo=ET),
            # free tier refuses SIP queries ending inside the last 15 min
            end=datetime.now(ET) - timedelta(minutes=20),
            feed="sip", adjustment=Adjustment.RAW, limit=None))
        rows = [{"ts": b.timestamp.astimezone(ET), "o": float(b.open),
                 "c": float(b.close)} for b in out.data[sym]]
        df = pd.DataFrame(rows)
        df.to_parquet(CACHE / f"under30_raw_{sym}.parquet")
        print(f"{sym}: {len(df)} raw 30m bars "
              f"{df['ts'].min().date()} .. {df['ts'].max().date()}")


def under_frame(sym: str) -> pd.DataFrame:
    df = pd.read_parquet(CACHE / f"under30_raw_{sym}.parquet")
    df["d"] = df["ts"].dt.date
    df["hm"] = df["ts"].dt.strftime("%H:%M")
    first = df[df["hm"] == "09:30"].set_index("d")["c"]   # price at 10:00
    last = df[df["hm"] == "15:30"].set_index("d")["c"]    # price at 16:00
    days = sorted(set(first.index) & set(last.index))
    return pd.DataFrame({"d": days,
                         "px1000": [float(first.loc[d]) for d in days],
                         "close": [float(last.loc[d]) for d in days]})


# ------------------------------------------------------------------- fetch

def _opt_client():
    from alpaca.data.historical.option import OptionHistoricalDataClient
    s = _settings()
    return OptionHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)


def _day_bars(api, symbols: list[str], d) -> dict[str, list]:
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame
    req = OptionBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
        start=datetime(d.year, d.month, d.day, 9, 55, tzinfo=ET),
        end=datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET))
    return api.get_option_bars(req).data


def _marks_from_bars(bars: list) -> dict[str, float | None]:
    """Leg price at each mark: first print at/after the mark (grace-limited
    for the entry mark, last-print-before for the afternoon marks)."""
    px = {}
    times = [(b.timestamp.astimezone(ET), float(b.close)) for b in bars]
    for hm in MARKS:
        h, mn = map(int, hm.split(":"))
        target = h * 60 + mn
        at_or_after = [(t, c) for t, c in times
                       if t.hour * 60 + t.minute >= target]
        if hm == "10:00":
            ok = [(t, c) for t, c in at_or_after
                  if t.hour * 60 + t.minute <= target + ENTRY_GRACE_MIN]
            px[hm] = ok[0][1] if ok else None
        else:
            before = [(t, c) for t, c in times
                      if t.hour * 60 + t.minute <= target]
            px[hm] = before[-1][1] if before else (
                at_or_after[0][1] if at_or_after else None)
    return px


def stage_probe(args) -> None:
    api = _opt_client()
    und = under_frame("SPY")
    mid = und[und["d"].astype(str) >= "2024-06-03"]
    row = mid.iloc[0] if len(mid) else und.iloc[-1]
    d, px = row["d"], row["px1000"]
    k = round(px)
    syms = [occ("SPY", d, "C", k), occ("SPY", d, "P", k)]
    print(f"probe day {d}: SPY@10:00 = {px:.2f}, strike {k}, legs {syms}")
    data = _day_bars(api, syms, d)
    for s_, bars in data.items():
        print(f"  {s_}: {len(bars)} minute bars; "
              f"marks {_marks_from_bars(bars)}")
    if not data:
        print("  EMPTY RESPONSE — check entitlement/feed before fetch")


def stage_fetch(args) -> None:
    api = _opt_client()
    prior = pd.DataFrame()
    out_f = CACHE / "straddle_marks.parquet"
    have = set()
    if out_f.exists():
        prior = pd.read_parquet(out_f)
        have = set(zip(prior["sym"], prior["d"].astype(str)))
    rows = []
    for sym in UNDERLYINGS:
        und = under_frame(sym)
        todo = [r for _, r in und.iterrows()
                if (sym, str(r["d"])) not in have]
        print(f"{sym}: {len(todo)} sessions to fetch")
        for n, r in enumerate(todo):
            d, px = r["d"], r["px1000"]
            k = float(round(px))
            legs = {right: occ(sym, d, right, k) for right in ("C", "P")}
            try:
                data = _day_bars(api, list(legs.values()), d)
            except Exception as exc:
                print(f"  {sym} {d}: {str(exc)[:70]}")
                _time.sleep(1.0)
                continue
            rec = {"sym": sym, "d": str(d), "k": k, "px1000": px,
                   "close": r["close"]}
            for right, s_ in legs.items():
                marks = _marks_from_bars(data.get(s_) or [])
                for hm, v in marks.items():
                    rec[f"{right}_{hm.replace(':', '')}"] = v
            rows.append(rec)
            if n % 50 == 0:
                print(f"  {sym} {n}/{len(todo)} ({d})")
            _time.sleep(0.25)
    df = pd.DataFrame(rows)
    allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
    allf.to_parquet(out_f)
    print(f"cached {len(allf)} straddle-days -> {out_f}")


# ------------------------------------------------------------------- score

def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def stage_score(args) -> None:
    df = pd.read_parquet(CACHE / "straddle_marks.parquet")
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# 0DTE ATM straddle: implied vs delivered — {STAMP}")
    emit()
    emit("Entry = first leg prints in [10:00, 10:10] ET; settle = expiry "
         "intrinsic |16:00 close - K|; prices are OPRA minute-bar trade "
         "closes, GROSS of spread (ATM 0DTE SPY quotes ~1-2c wide, "
         "~0.3-0.7% of premium, both sides pay it). Seller edge% = "
         "(premium - payoff) / premium.")
    emit()
    for sym in UNDERLYINGS:
        s = df[df["sym"] == sym].copy()
        n_all = len(s)
        s = s.dropna(subset=["C_1000", "P_1000"])
        prem = s["C_1000"] + s["P_1000"]
        payoff = (s["close"] - s["k"]).abs()
        pnl_bp = (prem - payoff) / s["px1000"] * 1e4          # seller, bp of S
        edge = (prem - payoff) / prem * 100
        implied = prem / s["px1000"] * 100
        realized = payoff / s["px1000"] * 100
        s["year"] = s["d"].str[:4]
        emit(f"## {sym} — {len(s)} sessions (of {n_all}; "
             f"{n_all - len(s)} lacked an entry print)")
        emit()
        emit(f"- implied move @10:00 (straddle/S): mean {implied.mean():.2f}%, "
             f"median {implied.median():.2f}%")
        emit(f"- delivered |move| 10:00-strike vs close: mean "
             f"{realized.mean():.2f}%, median {realized.median():.2f}%")
        emit(f"- SELLER: mean {pnl_bp.mean():+.1f}bp of S/day "
             f"(t {tstat(pnl_bp.to_numpy()):+.2f}), win% "
             f"{(pnl_bp > 0).mean() * 100:.0f}, edge {edge.mean():+.1f}% of "
             f"premium; worst day {pnl_bp.min():+.0f}bp, best "
             f"{pnl_bp.max():+.0f}bp")
        daily = pnl_bp.to_numpy() / 1e4
        emit(f"- seller ann Sharpe (gross, 1 straddle/day, bp-of-S basis): "
             f"{daily.mean() / daily.std(ddof=1) * np.sqrt(252):+.2f}")
        emit()
        emit("| year | n | implied % | delivered % | seller bp/d | t | win% | worst bp |")
        emit("|---|---|---|---|---|---|---|---|")
        for y, g in s.groupby("year"):
            pr = g["C_1000"] + g["P_1000"]
            po = (g["close"] - g["k"]).abs()
            pb = ((pr - po) / g["px1000"] * 1e4).to_numpy()
            emit(f"| {y} | {len(g)} | {(pr / g['px1000'] * 100).mean():.2f} "
                 f"| {(po / g['px1000'] * 100).mean():.2f} | {pb.mean():+.1f} "
                 f"| {tstat(pb):+.2f} | {(pb > 0).mean() * 100:.0f} "
                 f"| {pb.min():+.0f} |")
        emit()
        # Timing: what remains of the premium at each mark.
        for hm in ("1400", "1530"):
            cc, pp = s[f"C_{hm}"], s[f"P_{hm}"]
            m = cc.notna() & pp.notna()
            if m.sum() < 30:
                continue
            rem = (cc[m] + pp[m])
            pnl2 = (rem - (s["close"][m] - s["k"][m]).abs()) / s["px1000"][m] * 1e4
            label = f"{hm[:2]}:{hm[2:]}"
            emit(f"- sell at {label} instead (n={int(m.sum())}): "
                 f"{pnl2.mean():+.1f}bp of S/day (t {tstat(pnl2.to_numpy()):+.2f}, "
                 f"win% {(pnl2 > 0).mean() * 100:.0f})")
        emit()

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"straddle_0dte_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# ------------------------------------------------------------------- wings

WIDTH_PCTS = (0.3, 0.6, 1.0)          # wing distance as % of the underlying
WING_SYM = "QQQ"                      # the ticker where the premium lives


def stage_wings(args) -> None:
    """OTM wing legs for the fly sweep: C at K+w and P at K-w per width."""
    api = _opt_client()
    base = pd.read_parquet(CACHE / "straddle_marks.parquet")
    base = base[base["sym"] == WING_SYM]
    out_f = CACHE / "wing_marks.parquet"
    prior, have = pd.DataFrame(), set()
    if out_f.exists():
        prior = pd.read_parquet(out_f)
        have = set(prior["d"].astype(str))
    todo = base[~base["d"].astype(str).isin(have)]
    print(f"{WING_SYM}: {len(todo)} sessions need wings")
    rows = []
    for n, r in enumerate(todo.itertuples()):
        d = datetime.strptime(r.d, "%Y-%m-%d").date()
        legs = {}
        for wp in WIDTH_PCTS:
            w = max(1.0, float(round(r.px1000 * wp / 100)))
            legs[(wp, "C")] = (occ(WING_SYM, d, "C", r.k + w), r.k + w)
            legs[(wp, "P")] = (occ(WING_SYM, d, "P", r.k - w), r.k - w)
        try:
            data = _day_bars(api, [s for s, _ in legs.values()], d)
        except Exception as exc:
            print(f"  {r.d}: {str(exc)[:70]}")
            _time.sleep(1.0)
            continue
        rec = {"d": r.d, "k": r.k, "px1000": r.px1000, "close": r.close}
        for (wp, right), (s_, kw) in legs.items():
            marks = _marks_from_bars(data.get(s_) or [])
            rec[f"K_{right}_{wp}"] = kw
            for hm, v in marks.items():
                rec[f"{right}{wp}_{hm.replace(':', '')}"] = v
        rows.append(rec)
        if n % 50 == 0:
            print(f"  {n}/{len(todo)} ({r.d})")
        _time.sleep(0.25)
    df = pd.DataFrame(rows)
    allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
    allf.to_parquet(out_f)
    print(f"cached {len(allf)} wing-days -> {out_f}")


def stage_flies(args) -> None:
    """Iron flies: short the ATM straddle, long both wings. The sweep is
    entry time x wing width; every cell reports the seller's distribution
    AND return on the capital the broker would actually hold (max loss)."""
    atm = pd.read_parquet(CACHE / "straddle_marks.parquet")
    atm = atm[atm["sym"] == WING_SYM].set_index("d")
    wings = pd.read_parquet(CACHE / "wing_marks.parquet").set_index("d")
    j = atm.join(wings, how="inner", rsuffix="_w")
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# 0DTE iron flies on {WING_SYM} — {STAMP}")
    emit()
    emit(f"{len(j)} sessions. Short ATM straddle + long wings at K±w; "
         "settle at expiry intrinsic; prices are OPRA minute-bar closes, "
         "gross of ~2-4c/structure friction. ROC = P&L / (w - credit), the "
         "margin the broker holds. bp = P&L as bp of the underlying.")
    emit()
    emit("| entry | width | n | credit/w % | mean bp | t | win% | worst bp | ann Sharpe | mean ROC % |")
    emit("|---|---|---|---|---|---|---|---|---|---|")
    best = None
    for hm in ("1000", "1400", "1530"):
        for wp in WIDTH_PCTS:
            cols = [f"C_{hm}", f"P_{hm}", f"C{wp}_{hm}", f"P{wp}_{hm}"]
            if not all(c in j.columns for c in cols):
                continue
            g = j.dropna(subset=cols)
            if len(g) < 100:
                continue
            w = (g[f"K_C_{wp}"] - g["k"])
            credit = g[f"C_{hm}"] + g[f"P_{hm}"] - g[f"C{wp}_{hm}"] - g[f"P{wp}_{hm}"]
            s_t = g["close"]
            pay_atm = (s_t - g["k"]).abs()
            pay_cw = np.maximum(0.0, s_t - g[f"K_C_{wp}"])
            pay_pw = np.maximum(0.0, g[f"K_P_{wp}"] - s_t)
            pnl = credit - pay_atm + pay_cw + pay_pw
            bp = (pnl / g["px1000"] * 1e4).to_numpy()
            max_loss = np.maximum(w - credit, 0.01)
            roc = (pnl / max_loss * 100).to_numpy()
            sharpe = bp.mean() / bp.std(ddof=1) * np.sqrt(252)
            emit(f"| {hm[:2]}:{hm[2:]} | {wp}% | {len(g)} "
                 f"| {(credit / w * 100).mean():.0f} | {bp.mean():+.1f} "
                 f"| {tstat(bp):+.2f} | {(bp > 0).mean() * 100:.0f} "
                 f"| {bp.min():+.0f} | {sharpe:+.2f} | {roc.mean():+.1f} |")
            if best is None or tstat(bp) > best[0]:
                best = (tstat(bp), hm, wp, g, bp)
    emit()
    if best is not None:
        _, hm, wp, g, bp = best
        emit(f"## Best-t cell by year: {hm[:2]}:{hm[2:]} entry, {wp}% wings")
        emit()
        emit("| year | n | mean bp | t | win% | worst |")
        emit("|---|---|---|---|---|---|")
        yr = g.index.astype(str).str[:4]
        for y in sorted(set(yr)):
            x = bp[np.asarray(yr == y)]
            emit(f"| {y} | {len(x)} | {x.mean():+.1f} | {tstat(x):+.2f} "
                 f"| {(x > 0).mean() * 100:.0f} | {x.min():+.0f} |")
        emit()
        emit("Best-of-9-cells caveat applies: the year split is the honesty "
             "check, and any traded config gets pre-registered first.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"flies_0dte_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["under", "probe", "fetch", "score",
                                      "wings", "flies"])
    args = ap.parse_args()
    {"under": stage_under, "probe": stage_probe,
     "fetch": stage_fetch, "score": stage_score,
     "wings": stage_wings, "flies": stage_flies}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
