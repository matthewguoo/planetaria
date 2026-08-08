# chart-llm-gate

**Can a language model read a chart?** Five retail daytrading setups, twelve
liquid megacaps, one year of 5-minute bars, and a local model that is shown
the price action with the ticker, the date and the price level stripped out.

```
scripts/    one module per question
notes/      dated run logs
cache/      bars, setups, verdicts (gitignored)
```

---

## Why this study exists

`research/pead-llm-gate/` spent its entire second half fighting one problem:
when a model reads a named, dated earnings release and predicts what the stock
did next, you cannot tell reading from remembering. That study threw
everything at it — blind arms, text-free arms, a forced-recall probe, document
perturbation, four Opus generations with staggered cutoffs, two local models
from a different builder — and arrived at a memorisation bound of ±86bp that
is **supply-constrained**: every model with a pre-2025 cutoff has been
retired, so no amount of money narrows it further.

That entire apparatus exists because an earnings release is a *document with a
name*, and its consequences are written about at length.

A candle is not. Strip the identity and every bar becomes a percentage:

```
BAR  SESS  MIN    OPEN    HIGH     LOW   CLOSE   VOL
 -3     0    0   -1.20   -0.19   -1.39   -0.23   7.1
 -2     0    5   -0.23   -0.15   -0.79   -0.75   3.2
 -1     0   10   -0.75   -0.53   -1.00   -0.56   2.6
  0     0   15   -0.56   +0.19   -0.74   +0.00   3.3
```

No ticker, no date, no price, no share counts. `MIN` is minutes since 09:30 —
a within-day clock that identifies no particular day but lets an
opening-range setup mean something. `SESS` marks the session boundary so an
overnight gap reads as structure. The contamination problem does not get
bounded here; it **dissolves**.

### The honest limit, measured rather than asserted

This does not make the snapshot irreversible. `research_chart_render.py verify`
searches all 234,336 windows in the universe for the best match to a rendered
snapshot at the exact precision the model sees, and recovers `(symbol, date)`
**60 times out of 60**. A sequence of 47 rounded relative returns is
essentially unique.

The claim does not rest on irreversibility and does not need to:

1. **No corpus pairs a normalised candle table with what happened next.** That
   text was never written. Earnings outcomes, by contrast, are written about
   endlessly — which is exactly what made the PEAD study hard.
2. **A language model has no database to search.** Inverting the snapshot
   requires the bar database; next-token prediction is not a search over one.

So the available claim — much stronger than anything PEAD could buy at any
price — is that *recall of the outcome is unavailable, because outcome-paired
text for these windows does not exist*.

---

## The five setups

Parameters are what the setups are *taught* with, fixed before any result was
seen. Full definitions in `research_chart_setups.py`.

| | |
|---|---|
| `orb` | 15-minute opening range, first 5m close outside it. Stop at the opposite extreme. |
| `vwap` | Anchored session VWAP, five closes one side then a close through. |
| `ema` | 9/21 EMA crossover on closes. |
| `sweep` | Prior session's high/low taken out intrabar and closed back inside — fade it. |
| `flag` | ≥0.5% pole, 3-8 bars of consolidation inside 40% of its range, break in the pole's direction. |

All five: RTH entries 09:30–15:00 only, 2R target, 100-bar time stop, flat by
the close, stop wins intrabar ties, 2bp round-trip costs. Fixing the bracket
across strategies is what makes the random-entry null fair — it holds the
geometry constant so the only thing varying is where the entry came from.

`sweep` deliberately uses the **prior session's** extremes rather than the
overnight range tested as PO3 in
`research/pead-llm-gate/notes/ict_backtest_20260805.md`, so it is a different
level and not a rerun.

---

## The LLM arms

Each exists to kill a specific alternative explanation.

| arm | what it rules out |
|---|---|
| `outcome` | — **the measurement the conclusion rests on.** Given this chart and this bracket, does price reach the target or the stop first? Gate keeps on `target`. |
| `gate` | — the *intended* measurement. Model calls the chart bullish/bearish; gate keeps when that agrees with the flagged side. Turned out not to discriminate (see below), kept because the agreement rate is itself a result. |
| `shuffled` | **pipeline leakage.** Same proposal, same outcome, but the chart shown belongs to a *different* setup. The spread must collapse. If it survives, every other number is void. |
| `coldread` | **the gate's crutch.** No setup, no proposal — a random window and a forced call on the next 60 minutes. The direct test of whether chart reading is anything at all. |
| `synthetic` | **confidence that isn't tracking anything.** GBM at the universe's own volatility through the identical formatter. Accuracy is 50% by construction; the point is the *conviction distribution*. |
| `gateopt` | run at low volume purely to record the abstention rate. |

### The comprehension gate — run this before spending a night on any model

`research_chart_probe.py` asks the model questions about the same anonymised
chart that have **verifiable answers stated in the table**: the highest high,
the close of bar −10, how many of the last twelve bars closed up. No
forecasting, no judgement. Pure reading comprehension of the exact artefact
the reading arms hand it.

It exists because every null in this study has two explanations that predict
the same number — *chart reading carries no information*, or *this model
cannot parse a 48-row numeric table*. The qwen3 capability control answers
that indirectly and expensively. This answers it directly, in four minutes.

**Pre-registered bar: mean ≥ 85%, and every dimension ≥ 60%.** Written down
before any candidate was scored. A threshold picked afterwards is a threshold
picked to admit the model you liked.

**phi-4 Q8_0 FAILS, badly** (2026-08-07):

| question | correct |
|---|---|
| highest high in the window | 68.7% |
| lowest low | 78.7% |
| close of bar −10 | 74.7% |
| **how many of last 12 bars closed up** | **11.3%** |
| final close above/below VWAP | 91.3% |
| **mean** | **64.9%** |

It gets the highest number in a column wrong one time in three, and counting
twelve rows is at or below naive guessing. This is not quantisation damage —
Q8_0 is near-lossless — it is a 14B model.

**Consequence: every phi-4 number in this study is a capability floor, not a
finding.** The −8.45bp gate spread, the base-rate nulls and the 97% agreement
rate describe what a model that cannot read the table does. They say nothing
about whether charts are readable.

The error pattern also carries a real hint. The 91.3% on the VWAP question is
the one where the answer was **stated in the prompt** rather than requiring a
scan; the failures are all *aggregations over the table* — max, min, count.
So the model can use facts it is handed and cannot reliably compute facts it
is not. That reframes the context arm: its extra block states the session
high, the low, the ATR and the VWAP bands explicitly, so part of what the
ablation measures is not "does context add information" but **"does doing the
aggregation for the model rescue it"** — which is worth knowing either way,
and is why the arm reports how often context flips a verdict.

### Two instrument failures, both caught and both informative

**1. The escape hatch.** The first `gate` run returned `neutral`/`low` on
**48 of 48** charts. The gate vetoes every neutral, so it kept nothing.

Not new here: `research_local_gate.py` records the same model answering
`neutral` on ~83% of earnings releases against Opus's 16%, and "no recall" on
489 of 489 memory probes. Phi-4 is heavily hedge-tuned. **An arm with an
escape hatch measures willingness to commit, not ability to read.** So `gate`
forces a side, exactly as PEAD's `forced` and `notextf` arms do.

**2. The yes-man.** Forced to take a side, phi-4 then agreed with the screen's
proposal on **97%** of charts — 100% on three of the five strategies. That is
not a gate either.

The cause is structural rather than a bug: every setup here is a momentum or
breakout pattern, so the chart the model is shown *visibly contains the thing
the detector fired on*. Asked whether a chart that just broke out looks
bullish, a model says bullish. The verdict is nearly a deterministic function
of the setup's own direction.

Hence `outcome`, which asks the question that decides the money and which the
setup direction does **not** telegraph: target first, or stop first? A model
with no skill lands near the base rate; a model with skill separates the
trades that resolved at the target from the ones that resolved at the stop.

The 97% figure is kept and reported, because read against `shuffled` it is a
strong result on its own — **if the model agrees with the proposal just as
often when shown a completely different chart, its verdicts are not a function
of the chart at all.**

### Two models, because one is unreadable

A null from a single small model cannot be interpreted — "chart reading is
astrology" and "a 14B cannot parse a candle table" predict the same number.
**phi-4** (14B dense, Q8_0) carries the volume; **Qwen3-30B-A3B** (MoE, 4-bit)
re-scores a subset as a capability control.

---

## Three nulls for the gate spread

A raw spread proves nothing on its own.

- **permutation** — shuffle verdict labels within (strategy, month). Kills
  "no skill, but some months and strategies pay better."
- **shuffled arm** — empirical, end-to-end, causal link cut.
- **feature-matched** — a logistic regression on six numbers any chart-reader
  can see (trailing return, realised vol, position in day's range, relative
  volume, minutes since open, VWAP distance), fit **strictly walk-forward**
  and thresholded to keep the same fraction of trades the LLM kept.

The third is the one that decides what the result *means*. If a six-feature
logistic gates as well as a 14B language model, then "the model reads charts"
is the wrong description; "the model computes momentum, slowly" is the right
one.

---

## Runbook

```bash
cd research/chart-llm-gate
P=../../backend/.venv/Scripts/python.exe

$P scripts/research_chart_data.py fetch      # ~2.4M 1m SIP bars, 12 symbols
$P scripts/research_chart_data.py check      # coverage audit
$P scripts/research_chart_setups.py build    # -> cache/setups.parquet
$P scripts/research_chart_setups.py report   # the exhaustive mechanical table
$P scripts/research_chart_gate.py sample     # freeze the LLM panels
```

Then, with a server up in its own shell:

```bash
$P scripts/research_chart_gate.py serve --model phi4
```

and every arm in the order the study wants them (`outcome` first, so a night
that gets cut short still has the arm the conclusion rests on):

```bash
bash scripts/run_all.sh phi4
```

then swap the server to `qwen3` and run `run_all.sh qwen3`. Finally

```bash
$P scripts/research_chart_score.py
$P scripts/research_chart_account.py sweep
$P scripts/research_chart_account.py account
```

---

## Things that will bite

- **`--ctx-size` must be ≥ 3072 per slot.** Prompts measured at a 1,951-token
  median and a 2,021 max. The first config gave 2,048/slot and would have
  truncated the long tail silently.
- **The run lock is load-bearing.** `.lock_{arm}_{model}` in `cache/verdicts/`.
  A killed shell leaves an orphan python that keeps writing; two writers
  appending to one jsonl is how the PEAD run got 2,609 rows for 1,815 events
  with no symptom but a row count nobody was checking. Check for orphans
  before deleting a lock.
- **CAGR needs ≥90 days of span.** Annualising a 3-day panel reported
  +349,190% before the guard went in.
- **The account's daily curve must forward-fill.** Dropping no-trade days
  removes zeros from the return series and flatters Sharpe.
- **Read the bracket sweep as a surface, not a maximum.** 16 cells × 5
  strategies means the best of 80 looks good on noise alone.

## Provenance

`cache/bars/` is ~2.4M SIP minute bars and is regenerable in about two minutes
of API time. `cache/verdicts/` is not expensive in dollars but is expensive in
wall-clock — a full phi-4 pass over all five arms is ~4.3 hours on one 3090.
