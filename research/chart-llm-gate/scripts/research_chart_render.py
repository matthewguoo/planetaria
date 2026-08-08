"""The anonymiser: a chart the model can read and cannot look up.

THIS MODULE IS THE STUDY'S ONE REAL IDEA, so it is worth being precise about
what it buys and what it does not.

THE PROBLEM IT SOLVES. `research/pead-llm-gate/` spent its entire second half
trying to bound how much of an LLM's apparent edge on earnings releases was
memory of the outcome rather than reading of the text: blind arms, notext
arms, a forced-recall probe, document perturbation, four Opus generations
with staggered cutoffs, and finally two local models from a different builder.
The bound it reached was +-86bp and it is SUPPLY-CONSTRAINED — every model
with a pre-2025 cutoff has been retired, so no amount of money narrows it.
That whole apparatus exists because an earnings release is a named, dated
document whose consequences are written about at length in the training
corpus.

A candle is not. Strip the ticker, the date, and the price level, express
every bar as a percentage of the last close, and the artefact that remains
has no key to look anything up by. So:

    BAR   SESS   MIN    OPEN    HIGH     LOW   CLOSE   VOL
    -12      0    75   +0.14   +0.18   +0.06   +0.09   1.3
      0      0   135   -0.02   +0.03   -0.07    0.00   2.4

WHAT IS DELIBERATELY ABSENT: symbol, date, day of week, absolute price,
share volume, and any string that could name the instrument. Prices are
percentages from the decision bar's close (which is therefore always exactly
0.00), volume is a ratio to the window's own median, and the only clock is
MIN — minutes since 09:30 — which is a within-day quantity that identifies no
particular day. SESS is 0 for the current session and -1 for the one before,
so an overnight gap is visible as structure rather than as a mystery jump.

WHAT IS DELIBERATELY PRESENT: session structure. An opening-range setup is
meaningless if the model cannot tell it is looking at the open, and removing
MIN would handicap the reading arms to buy an anonymity the study already
has. MIN leaks nothing about WHICH day.

THE HONEST LIMIT, MEASURED RATHER THAN ASSERTED. This does not make the
snapshot information-theoretically irreversible. A sequence of 48 rounded
relative returns is close to unique, and anyone holding the bar database can
in principle search it and recover (symbol, date). `verify` does exactly that
and reports the rank-1 recovery rate, so the number is on the record instead
of being a hand-wave.

The contamination claim does not rest on irreversibility, and it does not
need to. It rests on two things:

  1. No corpus anywhere pairs a normalised relative-candle table with what
     happened next. That text does not exist. Earnings outcomes, by contrast,
     are written about endlessly — which is precisely what made the PEAD study
     hard and this one easy.
  2. The model has no retrieval mechanism over a bar database at inference
     time. Inverting the snapshot requires the database; next-token
     prediction is not a search over one.

So the strong claim available here — much stronger than anything the PEAD
study could buy at any price — is that recall of the OUTCOME is unavailable,
because outcome-paired text for these windows was never written.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

WINDOW = 48             # bars shown; at 5m that is 4 hours of tape
VOL_FLOOR = 1e-9


def _pct(x: float, anchor: float) -> float:
    return round((x / anchor - 1) * 100, 2)


def snapshot(df: pd.DataFrame, i: int, window: int = WINDOW) -> dict:
    """The anonymised chart ending at bar i, plus the anchor it was built on.

    NO-LOOKAHEAD GUARANTEE, and it is structural rather than a convention:
    the slice is `[i - window + 1 : i + 1]`, so bar i is the last row and
    nothing after it is reachable from here. Every caller passes the same `i`
    the exit engine starts walking from at `i + 1`. There is no path by which
    a future bar enters this text.
    """
    lo = max(0, i - window + 1)
    w = df.iloc[lo:i + 1]
    anchor = float(df["c"].iat[i])
    vmed = float(np.median(w["v"].to_numpy())) or VOL_FLOOR
    day_i = df["day"].iat[i]

    rows = ["BAR  SESS  MIN    OPEN    HIGH     LOW   CLOSE   VOL"]
    for k, (_, b) in enumerate(w.iterrows()):
        # Sessions are numbered backwards from the decision bar's own day, so
        # 0 is always "today" regardless of where the window started.
        sess = -int(np.busday_count(b["day"].date(), day_i.date()))
        rows.append(
            f"{k - (len(w) - 1):>3}  {sess:>4}  {int(b['min']):>3}  "
            f"{_pct(b['o'], anchor):>+6.2f}  {_pct(b['h'], anchor):>+6.2f}  "
            f"{_pct(b['l'], anchor):>+6.2f}  {_pct(b['c'], anchor):>+6.2f}  "
            f"{b['v'] / vmed:>4.1f}"
        )
    return {"table": "\n".join(rows), "anchor": anchor, "bars": len(w)}


# ------------------------------------------------------- setup-specific context
#
# Each strategy's defining LEVELS, restated in the same anonymised units as
# the table. Without these the model would have to re-derive the opening
# range or the VWAP from the bars, which is a test of arithmetic rather than
# of chart reading — and it would score the setups unequally, since a VWAP is
# far harder to recover by eye than an opening range.

def _context(row: pd.Series, df: pd.DataFrame, anchor: float) -> list[str]:
    i = int(row["i"])
    b = df.iloc[i]
    out = []
    st = row["strategy"]
    if st == "orb":
        out += [f"opening range (09:30-09:45): high {_pct(b['orh'], anchor):+.2f}%, "
                f"low {_pct(b['orl'], anchor):+.2f}%"]
    elif st == "vwap":
        out += [f"session VWAP: {_pct(b['vwap'], anchor):+.2f}%"]
    elif st == "ema":
        out += [f"9-EMA {_pct(b['ema9'], anchor):+.2f}%, "
                f"21-EMA {_pct(b['ema21'], anchor):+.2f}%"]
    elif st == "sweep":
        out += [f"prior session high {_pct(b['pdh'], anchor):+.2f}%, "
                f"low {_pct(b['pdl'], anchor):+.2f}%"]
    if not pd.isna(b.get("vwap")) and st != "vwap":
        out += [f"session VWAP: {_pct(b['vwap'], anchor):+.2f}%"]
    return out


SETUP_BLURB = {
    # Neutral, mechanical restatements. The temptation is to write these the
    # way the setups are TAUGHT ("a textbook bull flag continuation"), which
    # would be scoring the adjective rather than the chart.
    "orb": "the first 5-minute close outside the 15-minute opening range",
    "vwap": "a close back through session VWAP after five consecutive closes "
            "on the other side",
    "ema": "a 9/21 EMA crossover on closes",
    "sweep": "an intrabar push through the prior session's extreme that closed "
             "back inside on the same bar (a fade of the sweep)",
    "flag": "a break out of a tight consolidation that followed a >=0.5% "
            "impulse, in the impulse's direction",
}


def render_gate(row: pd.Series, df: pd.DataFrame,
                window: int = WINDOW) -> str:
    """The gate task: here is a chart and a proposed trade — take it or not."""
    snap = snapshot(df, int(row["i"]), window)
    a = snap["anchor"]
    side = "LONG" if row["side"] > 0 else "SHORT"
    ctx = _context(row, df, a)
    return "\n".join([
        "Below is a price chart. Each row is one 5-minute candle. All prices "
        "are percentages relative to the final bar's close, so the final "
        "close is exactly 0.00. VOL is that bar's volume as a multiple of the "
        "window's median volume. MIN is minutes since the 09:30 session open; "
        "SESS is 0 for the current session and -1 for the previous one.",
        "",
        snap["table"],
        "",
        f"A mechanical screen has flagged a {side} entry at the final bar's "
        f"close, on {SETUP_BLURB[row['strategy']]}.",
        f"  stop   {_pct(row['stop'], a):+.2f}%",
        f"  target {_pct(row['target'], a):+.2f}%",
        *([f"  {c}" for c in ctx] if ctx else []),
        "",
        "The trade is exited at whichever of the stop or the target is "
        "touched first, at the end of the session, or after 100 bars, "
        "whichever comes first.",
        "",
        "Judge the chart. Does the price action support taking this trade in "
        "the flagged direction, oppose it, or say nothing either way?",
    ])


def render_coldread(df: pd.DataFrame, i: int, horizon: int,
                    window: int = WINDOW) -> str:
    """The cold read: no setup, no proposal, just the tape and a forced call.

    This is the analogue of the PEAD study's `notextf` probe, and it exists
    for the same reason: an arm where the model has no escape hatch, so a
    model with nothing to say scores exactly 50% instead of scoring the base
    rate of whatever it abstains into.
    """
    snap = snapshot(df, i, window)
    return "\n".join([
        "Below is a price chart. Each row is one 5-minute candle. All prices "
        "are percentages relative to the final bar's close, so the final "
        "close is exactly 0.00. VOL is that bar's volume as a multiple of the "
        "window's median volume. MIN is minutes since the 09:30 session open; "
        "SESS is 0 for the current session and -1 for the previous one.",
        "",
        snap["table"],
        "",
        f"Read the chart. Over the next {horizon} bars ({horizon * 5} "
        f"minutes), does price close higher or lower than it is now?",
    ])


# ---------------------------------------------------------------- synthetic
#
# A random walk with no structure whatsoever, rendered through the identical
# formatter. If the model's confidence and verdict distribution on these is
# indistinguishable from its distribution on real charts, then those verdicts
# are not tracking anything in the real charts either — which is a stronger
# and much cheaper statement than anything the outcome data can support,
# because a GBM path is KNOWN to contain no signal.

def synthetic_frame(rng: np.random.Generator, n_bars: int,
                    bar_vol_bp: float, start_min: int = 60) -> pd.DataFrame:
    """A GBM path aggregated into 5m OHLC, with volatility matched to the
    real universe. Each 5m bar is built from 5 one-minute sub-steps so the
    highs and lows are the extremes of an actual path rather than drawn
    independently, which would produce candles no tape ever makes."""
    steps = rng.normal(0, bar_vol_bp / 1e4 / np.sqrt(5), size=n_bars * 5)
    px = 100 * np.exp(np.cumsum(steps))
    px = px.reshape(n_bars, 5)
    # Lognormal volume ratios, tuned to look like the real median-normalised
    # distribution (most bars near 1x, an occasional 4-5x burst).
    vol = rng.lognormal(0.0, 0.55, size=n_bars)
    return pd.DataFrame({
        "o": px[:, 0], "h": px.max(axis=1), "l": px.min(axis=1),
        "c": px[:, -1], "v": vol,
        "min": np.arange(n_bars) * 5 + start_min,
        "day": pd.Timestamp("2000-01-03"),      # never rendered; SESS is all 0
    })


# ------------------------------------------------------------------- verify

def verify_anonymity(setups: pd.DataFrame, frames: dict, n: int,
                     rng: np.random.Generator) -> dict:
    """How recoverable is (symbol, date) from a snapshot, for an adversary
    who HOLDS the bar database?

    Measured, not assumed. The answer is expected to be "very" — a sequence
    of 48 rounded relative returns is close to unique — and reporting that
    honestly is the point. It bounds the claim to the one the study actually
    needs: not that the snapshot is irreversible, but that no outcome-paired
    text for these windows exists to be memorised, and that a language model
    has no database to search even if it did.
    """
    from numpy.lib.stride_tricks import sliding_window_view

    # One flat corpus of every WINDOW-length return sequence in the universe,
    # with a parallel index recording where each came from. Built once: the
    # loop-per-candidate version is ~500M interpreted operations and does not
    # finish, which is how this started life.
    L = WINDOW - 1
    mats, owner_sym, owner_i = [], [], []
    for sym, df in frames.items():
        lr = np.diff(np.log(df["c"].to_numpy()))
        if len(lr) < L:
            continue
        mats.append(sliding_window_view(lr, L))
        # Row r of the view spans lr[r : r+L], i.e. closes i-WINDOW+1..i for
        # a decision bar i = r + L.
        owner_i.append(np.arange(len(lr) - L + 1) + L)
        owner_sym.append(np.full(len(lr) - L + 1, sym))
    corpus = np.vstack(mats)
    owner_i = np.concatenate(owner_i)
    owner_sym = np.concatenate(owner_sym)

    picks = setups[setups["i"] >= WINDOW].sample(
        min(n, int((setups["i"] >= WINDOW).sum())),
        random_state=int(rng.integers(1e6)))
    hits = 0
    for _, row in picks.iterrows():
        sym, i = row["symbol"], int(row["i"])
        lr = np.diff(np.log(frames[sym]["c"].to_numpy()))
        # The query is the precision the MODEL sees — the table is printed to
        # two decimal places of percent, i.e. 1e-4 in log-return terms. If the
        # snapshot is recoverable at all it must be recoverable at exactly
        # that precision, so the quantisation is part of the test.
        q = np.round(lr[i - L:i] * 1e4) / 1e4
        d = np.abs(corpus - q).sum(axis=1)
        j = int(np.argmin(d))
        if owner_sym[j] == sym and abs(int(owner_i[j]) - i) <= 1:
            hits += 1
    return {"tested": len(picks), "rank1_recovered": hits,
            "corpus_windows": int(len(corpus)),
            "symbols": int(len(frames))}
