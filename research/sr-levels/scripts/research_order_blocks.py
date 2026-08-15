"""The order-block meme, mechanised — and the stop-placement sweep.

TWO QUESTIONS, ONE MODULE. The prompt this time is the ICT/"smart money"
panel set: a DEMAND order block under a consolidation that price returns to
and launches from, its SUPPLY mirror, the OB FAIL where an "aggressive
pullback" smashes through the zone, and the consolidation variant. Plus a
follow-up on the whole study: the first note placed every stop exactly where
the memes draw them — AT the pattern line — and the trendline died OF ITS
STOP (65% stopped on a 12bp median R). So: do "fancier" stops — swings,
actual support pivots, ATR buffers — rescue any of these entries?

WHAT THIS INHERITS. The archived ICT harness
(`pead-llm-gate/notes/ict_backtest_20260805.md`) already killed two setups
from this same school: iFVG measured random-minus-costs and the PO3
sweep-reclaim fade was actively anti-predictive. Order blocks are the third
pillar of that canon and the one the archive never tested. Everything
mechanical here — bars, bracket engine, costs, the random-entry null — is
imported from `research_sr_levels.py` so the two notes read as one study.

THE ORDER BLOCK, AS TAUGHT. "The last opposite candle before displacement."
Mechanically:

  ob      candle j closes against the coming move (down candle for a bull
          OB); within 3 bars price CLOSES beyond j's extreme with total
          progress >= 2x ATR(14) — that close is the displacement, and
          [low_j, high_j] becomes the zone. First later touch of the zone
          that closes back out in the OB's direction: enter, stop past the
          zone's far edge, 2R target. One retest per block.
  obfail  the meme's third panel: a formed OB whose zone price CLOSES
          through instead of respecting. Enter the break direction at that
          close, stop at the zone's opposite edge, 2R. (This is the same
          trade the first note's `breakout` makes, at a different level —
          if respected OBs pay, their failures breaking even would be odd.)

Zones and retests are same-session, like every level in this study: the
overnight gap re-prices whatever institutional interest a 5m block is
supposed to mark. Displacement/zone parameters are the taught ones (2x ATR
"impulsive move", full-range zone, close-based break) and none moved after
seeing a result.

THE STOP SWEEP. Entries are HELD FIXED — every (bar, side) the five
detectors produced, meme setups and order blocks alike — and only the stop
policy varies:

  line     the meme's: at the pattern line / zone edge (the baseline)
  swing5   the 5-bar swing extreme — what chart-llm-gate's setups used
  pivot    the nearest CONFIRMED 2-bar-fractal support below entry (longs;
           resistance above for shorts), same session: "under the support,
           not under your entry". Falls back to swing5 when none exists.
  atr05    line minus 0.5 x ATR(14) of breathing room
  atr10    line minus 1.0 x ATR(14)

The 2R target is re-derived from each policy's stop (a wider stop moves the
target too — that is what fixed-RR sizing means), except `zone`, which keeps
its zone-to-zone target and lets RR float. Same 2bp costs, same tie rule.
This isolates the one question the meme format never argues: WHERE the stop
goes, holding WHEN you enter constant.

Run:
    python scripts/research_order_blocks.py build     # -> cache/ob_setups.parquet
    python scripts/research_order_blocks.py report    # OB note: table + null
    python scripts/research_order_blocks.py stops     # the sweep note
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import research_sr_levels as S  # noqa: E402

CACHE, NOTES, STAMP = S.CACHE, S.NOTES, S.STAMP

DISP_BARS = 3           # displacement must land within this many bars
DISP_ATR = 2.0          # ...and travel at least this many ATR(14)
ATR_N = 14

OB_STRATS = ["ob", "obfail"]
POLICIES = ["line", "swing5", "pivot", "atr05", "atr10"]


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)),
                                      np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    return pd.Series(tr).rolling(ATR_N, min_periods=ATR_N).mean().to_numpy()


# -------------------------------------------------------------- detector

def _sig_ob(df: pd.DataFrame) -> list[tuple[str, int, int, float, float]]:
    """(strategy, entry bar, side, stop, zone edge crossed). One retest and
    one fail per block, whichever the tape serves; a block dies with its
    session."""
    o, h, l, c = (df[k].to_numpy() for k in ("o", "h", "l", "c"))
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    atr = _atr(h, l, c)
    out, taken = [], set()
    n = len(df)
    for j in range(n):
        if not np.isfinite(atr[j]):
            continue
        for ob_side in (1, -1):
            against = c[j] < o[j] if ob_side > 0 else c[j] > o[j]
            if not against:
                continue
            top, bot = h[j], l[j]
            formed = 0
            for k in range(j + 1, min(j + 1 + DISP_BARS, n)):
                if days[k] != days[j]:
                    break
                if ob_side > 0 and c[k] > top and c[k] - c[j] >= DISP_ATR * atr[j]:
                    formed = k
                    break
                if ob_side < 0 and c[k] < bot and c[j] - c[k] >= DISP_ATR * atr[j]:
                    formed = k
                    break
            if not formed:
                continue
            # walk forward: first touch of the zone resolves as retest
            # (close back out the near edge) or fail (close through the far
            # edge); a close inside the zone keeps the question open
            near, far = (top, bot) if ob_side > 0 else (bot, top)
            touched = False
            for i in range(formed + 1, n):
                if days[i] != days[j]:
                    break
                hit = l[i] <= top if ob_side > 0 else h[i] >= bot
                if not (touched or hit):
                    continue
                touched = True
                if not (S.ENTRY_LO <= mins[i] <= S.ENTRY_HI):
                    if c[i] > top or c[i] < bot:
                        break               # resolved outside the entry window
                    continue
                if ob_side > 0 and c[i] > top:
                    if ("ob", i, 1) not in taken:
                        out.append(("ob", i, 1, float(bot), float(near)))
                        taken.add(("ob", i, 1))
                    break
                if ob_side > 0 and c[i] < bot:
                    if ("obfail", i, -1) not in taken:
                        out.append(("obfail", i, -1, float(top), float(far)))
                        taken.add(("obfail", i, -1))
                    break
                if ob_side < 0 and c[i] < bot:
                    if ("ob", i, -1) not in taken:
                        out.append(("ob", i, -1, float(top), float(near)))
                        taken.add(("ob", i, -1))
                    break
                if ob_side < 0 and c[i] > top:
                    if ("obfail", i, 1) not in taken:
                        out.append(("obfail", i, 1, float(bot), float(far)))
                        taken.add(("obfail", i, 1))
                    break
    return out


# ------------------------------------------------------------------ build

def stage_build(args) -> None:
    frames = []
    for sym in (args.symbols.split(",") if args.symbols else S.UNIVERSE):
        raw = S.load_bars(sym)
        if raw is None:
            print(f"  {sym}: no bars — run research_sr_levels.py fetch")
            continue
        df = S.prep(raw)
        h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
        day = df["day"].to_numpy()
        rows = []
        for name, i, side, stop, _edge in _sig_ob(df):
            entry = float(c[i])
            r = abs(entry - stop)
            if r <= 0 or (r / entry) * 1e4 < S.MIN_R_BP:
                continue
            tgt = entry + side * S.TP_R * r
            res, j, why = S.run_exit(h, l, c, day, i, side, entry, stop, tgt)
            rows.append({
                "symbol": sym, "strategy": name, "ts": df["ts"].iat[i],
                "date": str(pd.Timestamp(day[i]).date()), "i": i,
                "side": side, "entry": entry, "stop": stop, "target": tgt,
                "r_bp": (r / entry) * 1e4, "tp_r": S.TP_R,
                "gross_bp": (res / entry) * 1e4,
                "exit_i": j, "exit_ts": df["ts"].iat[j],
                "exit_px": entry + side * res,
                "exit_reason": why, "held": j - i,
            })
        d = pd.DataFrame(rows)
        if len(d):
            print(f"{sym}: {len(d):,} setups  "
                  + " ".join(f"{k}={v}" for k, v in
                             d["strategy"].value_counts().sort_index().items()),
                  flush=True)
            frames.append(d)
    if not frames:
        raise SystemExit("no setups built")
    out = pd.concat(frames, ignore_index=True).sort_values(["ts", "symbol"])
    out["net_bp"] = out["gross_bp"] - S.COST_BP
    out = out.reset_index(drop=True)
    out.to_parquet(CACHE / "ob_setups.parquet")
    print(f"\nwrote {len(out):,} setups -> ob_setups.parquet "
          f"({out['date'].min()}..{out['date'].max()})")


# ----------------------------------------------------------------- report

def stage_report(args) -> None:
    s = pd.read_parquet(CACHE / "ob_setups.parquet")
    rng = np.random.default_rng(20260815)
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Order blocks on 5m: retest and fail — {STAMP}")
    emit()
    emit(f"{len(s):,} setups, {s['symbol'].nunique()} symbols, "
         f"{s['date'].min()}..{s['date'].max()}, same harness as the levels "
         f"note ({S.COST_BP:.0f}bp costs, 2R, stop wins ties, flat by "
         "close). `ob` = zone respected (enter with the block, stop past "
         "its far edge); `obfail` = zone closed through (enter the break, "
         "stop at the opposite edge). The archive's prior on this school: "
         "iFVG random, PO3 fade inverted.")
    emit()
    emit("| strategy | trades | win% | mean net bp | t | median R bp | "
         "target% | stop% | null pctile |")
    emit("|---|---|---|---|---|---|---|---|---|")
    for name in OB_STRATS:
        g = s[s["strategy"] == name]
        if g.empty:
            continue
        net = g["net_bp"].to_numpy()
        nulls = []
        for sym, gs in g.groupby("symbol"):
            nulls.append(S.random_null(sym, len(gs), float(gs["r_bp"].mean()),
                                       float(gs["tp_r"].mean()), rng) * len(gs))
        pooled = np.sum(nulls, axis=0) / len(g)
        pct = float((pooled < net.mean()).mean() * 100)
        rc = g["exit_reason"].value_counts(normalize=True) * 100
        emit(f"| {name} | {len(g):,} | {(net > 0).mean() * 100:.1f} | "
             f"{net.mean():+.2f} | {S.tstat(net):+.2f} | "
             f"{g['r_bp'].median():.0f} | {rc.get('target', 0):.0f} | "
             f"{rc.get('stop', 0):.0f} | {pct:.1f} |")
    emit()
    emit("| symbol | " + " | ".join(OB_STRATS) + " |")
    emit("|---" * (len(OB_STRATS) + 1) + "|")
    piv = s.pivot_table(index="symbol", columns="strategy", values="net_bp",
                        aggfunc=["mean", "count"])
    for sym in sorted(s["symbol"].unique()):
        cells = []
        for st in OB_STRATS:
            try:
                m, n = piv[("mean", st)][sym], piv[("count", st)][sym]
                cells.append(f"{m:+.1f} ({int(n)})" if pd.notna(m) else "—")
            except KeyError:
                cells.append("—")
        emit(f"| {sym} | " + " | ".join(cells) + " |")
    emit()
    emit("| strategy | 0bp | 2bp | 5bp | 10bp |")
    emit("|---|---|---|---|---|")
    for name in OB_STRATS:
        g = s[s["strategy"] == name]["gross_bp"]
        if g.empty:
            continue
        emit(f"| {name} | " + " | ".join(f"{g.mean() - k:+.2f}"
                                         for k in (0, 2, 5, 10)) + " |")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"ob_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# ------------------------------------------------------------- stop sweep

def _pivot_stop(df: pd.DataFrame, i: int, side: int) -> float | None:
    """Nearest confirmed same-session fractal on the protective side of the
    entry close: the 'under the support' stop."""
    days = df["day"].to_numpy()
    s0 = S._session_start(days, i)
    entry = float(df["c"].iat[i])
    if side > 0:
        piv = S._pivots(df["l"].to_numpy(), s0, i, "lo")
        vals = [float(df["l"].iat[p]) for p in piv
                if float(df["l"].iat[p]) < entry]
        return max(vals) if vals else None
    piv = S._pivots(df["h"].to_numpy(), s0, i, "hi")
    vals = [float(df["h"].iat[p]) for p in piv
            if float(df["h"].iat[p]) > entry]
    return min(vals) if vals else None


def _swing_stop(df: pd.DataFrame, i: int, side: int) -> float:
    lo = max(0, i - 4)
    return (float(df["l"].iloc[lo:i + 1].min()) if side > 0
            else float(df["h"].iloc[lo:i + 1].max()))


def stage_stops(args) -> None:
    base = pd.concat([pd.read_parquet(CACHE / "setups.parquet"),
                      pd.read_parquet(CACHE / "ob_setups.parquet")],
                     ignore_index=True)
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# The stop sweep: same entries, five stops — {STAMP}")
    emit()
    emit(f"{len(base):,} entries from the levels + order-block notes, held "
         "fixed; only the stop policy moves, and the 2R target moves with "
         "it (`zone` keeps its zone-to-zone target and lets RR float). "
         "line = the meme's stop (the baseline the first notes measured). "
         f"{S.COST_BP:.0f}bp costs. If stop placement is what kills these "
         "patterns, some column other than `line` goes green; if the ENTRY "
         "is what's dead, every column agrees.")
    emit()
    emit("| strategy | policy | n | median R bp | win% | target% | stop% | "
         "mean net bp | t |")
    emit("|---|---|---|---|---|---|---|---|---|")

    frames, atr_cache = {}, {}
    for sym in base["symbol"].unique():
        frames[sym] = S._prep_cached(sym)
        f = frames[sym]
        atr_cache[sym] = _atr(f["h"].to_numpy(), f["l"].to_numpy(),
                              f["c"].to_numpy())

    for name in S.STRATEGIES + OB_STRATS:
        g = base[base["strategy"] == name]
        if g.empty:
            continue
        for pol in POLICIES:
            res_bp, reasons, r_bps = [], [], []
            for r in g.itertuples():
                df = frames[r.symbol]
                h, l, c = (df["h"].to_numpy(), df["l"].to_numpy(),
                           df["c"].to_numpy())
                day = df["day"].to_numpy()
                i, side, entry = int(r.i), int(r.side), float(r.entry)
                if pol == "line":
                    stop = float(r.stop)
                elif pol == "swing5":
                    stop = _swing_stop(df, i, side)
                elif pol == "pivot":
                    stop = _pivot_stop(df, i, side) or _swing_stop(df, i, side)
                else:
                    buf = {"atr05": 0.5, "atr10": 1.0}[pol] * atr_cache[r.symbol][i]
                    if not np.isfinite(buf):
                        continue
                    stop = float(r.stop) - side * buf
                rr = abs(entry - stop)
                if side * (entry - stop) <= 0 or (rr / entry) * 1e4 < S.MIN_R_BP:
                    continue
                if name == "zone":
                    tgt = float(r.target)
                    if side * (tgt - entry) <= 0:
                        continue
                else:
                    tgt = entry + side * S.TP_R * rr
                res, _, why = S.run_exit(h, l, c, day, i, side, entry, stop, tgt)
                res_bp.append((res / entry) * 1e4 - S.COST_BP)
                reasons.append(why)
                r_bps.append(rr / entry * 1e4)
            x = np.asarray(res_bp)
            if len(x) < 30:
                continue
            rc = pd.Series(reasons).value_counts(normalize=True) * 100
            emit(f"| {name} | {pol} | {len(x):,} | {np.median(r_bps):.0f} | "
                 f"{(x > 0).mean() * 100:.1f} | {rc.get('target', 0):.0f} | "
                 f"{rc.get('stop', 0):.0f} | {x.mean():+.2f} | "
                 f"{S.tstat(x):+.2f} |")
        emit("| | | | | | | | | |")
    emit()
    emit("Reading guide: `stop%` falling while `mean net bp` stays red means "
         "the wider stop converts stop-outs into session_end bleed — the "
         "loss moved, it didn't leave. A policy only 'works' if its mean "
         "clears zero with a t that survives this N.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"stops_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["build", "report", "stops"])
    ap.add_argument("--symbols", default=None, help="comma list; default all")
    args = ap.parse_args()
    {"build": stage_build, "report": stage_report,
     "stops": stage_stops}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
