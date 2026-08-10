# Handoff — the contamination question, 2026-08-07

Read this before touching the paper. It supersedes the reassurance in
`llm_contamination_20260806_0052.md` §"what this does NOT control for" and it
changes what §5.7 and Appendix B can claim.

---

## HEADLINE: what alpha to expect

**Do not size on the backtest. Expect the reading channel to be worth
somewhere between a 0.3 and a 1.3 Sharpe live, and understand that the panel
cannot narrow that range.**

The flagship's 2.148 (paper) / 2.13 (recomputed at measured 23.2bp fills) is
not a live expectation. Three deflators sit between it and production, in
descending order of how well they are established:

| deflator | status | size |
|---|---|---|
| memorisation | measured, magnitude disputed | up to −1.7 Sharpe |
| config selection | visible in your own tables | ~1.0 Sharpe (`shipped` 0.63 vs `best` 1.70) |
| fills at 23.2bp not 13bp | measured | −0.13 Sharpe |
| 2026 regime | observed, 7 months | −0.9 Sharpe |

**The one number that decides it** is the drift hit rate on events after
Opus 5's 2026-05 cutoff — whether the verdict predicts the sign of `fwd_bp`.
In-corpus it is 57.6%. If it holds near that live, the strategy is roughly
what the paper says minus config shrinkage. If it goes to 50%, the edge was
recall and there is nothing to trade. **The panel contains 37 such events.
This is not answerable from stored data.**

---

## What was built

Local inference on the 3090, so the study is no longer confined to one
vendor's corpus lineage.

- `llama.cpp` b10319 CUDA 12.4 at `C:\Users\matth\llamacpp` (driver 576.28
  caps at CUDA 12.9 — do not take the 13.x build).
- `C:\Users\matth\models\phi-4.Q8_0.gguf` (15.6GB) and
  `qwen3-30b-a3b.UD-Q4_K_XL.gguf` (17.7GB).
- `research_local_gate.py` — four arms (`notext`, `notextf`, `named`,
  `forced`), `serve`/`run`/`score`, a `_RunLock`, and a dedupe guard.
- `research_exit_rules.py` — 171-rule exit sweep, train/test split.
- `research_exit_curves.py` — equity curves per model per exit rule.
- `research_notextf_opus.py` — the positive control ($6.40 spent).
- `run_local_gate.sh` — unattended driver, resumable.
- `MODELS` in `research_llm_contamination.py` gained `phi4` and `qwen3`.
  `COHORT` in `research_out_of_training.py` is pinned, so nothing existing
  moved — verified by re-running both.

~14,500 local verdicts + 1,804 API verdicts. Total spend: $6.40.

---

## What was measured

### The memory probes

`notext` (abstention allowed) is **unusable on open models**. Phi-4 and
Qwen3 both returned "no recall / unknown" on 1,815 of 1,815. A no-schema
control showed Phi-4 refusing Microsoft's FY2023 revenue while correctly
describing Meta's post-Q4-2021 crash — it has memory and a refusal habit,
and this arm cannot separate them.

`notextf` (forced up/down, **sampled** at temp 1.0 — greedy collapses to a
constant) is the working instrument, and it is **validated**:

| model | forced-probe accuracy | z |
|---|---|---|
| **Opus 5** | **57.1%** (n=1789) | **+6.03** |
| Phi-4 | 48.6% | −1.23 |
| Qwen3 | 50.1% | +0.05 |

The Opus figure was **predicted at 56.7%** beforehand from the free-response
arm (41.4% volunteer rate × 66.3% accuracy + coin flips). It landed 0.4pp
away. The probe detects recall when recall exists, so the local nulls mean
what they appear to mean. Opus by confidence: low 52.9% (n=1438), medium
71.0% (n=269), **high 85.4% (n=82)**.

### The reading arms, 1,787 paired events

| model | neutral | predicts tape | predicts drift | gate spread | perm p |
|---|---|---|---|---|---|
| Opus 5 | 17% | 74.5% (z=+18.9) | **57.6%** (z=+5.88) | +278.9bp | 0.000 |
| Phi-4 | 71% | 67.8% (z=+8.06) | **48.3%** (z=−0.75) | +12.2bp | 0.288 |
| Qwen3 | 59% | 61.8% (z=+6.44) | **47.5%** (z=−1.36) | −13.8bp | 0.608 |

**The two accuracies are not the same question and conflating them wasted
most of a day.** Tape agreement is what `_gate` keys on; drift is what pays.
Both open models read the tape well above chance and predict the drift at
exactly chance. Only Opus predicts drift.

Agreement: Opus↔Phi 84.9%, Opus↔Qwen 80.8%, **Phi↔Qwen 98.7%**. The two open
models are running the same shallow heuristic; Opus is doing something else.

---

## The two readings that do not reconcile

### Reading A — the edge is largely memorisation

- A memory-only strategy (ticker + date, no filing) backtests at **Sharpe
  1.71 / 28.3% CAGR** against the document-driven flagship's 2.13 / 37.7%.
- On the same 1,481 events, the document adds **−0.3pp (se 1.8)** to drift
  prediction over memory alone.
- Document verdict where memory agrees (n=1,170): Sharpe **2.09**. Where
  memory disagrees (n=311): Sharpe **0.27**, drift accuracy 49.2%.
- Opus's recall is real and calibrated (85.4% on its high-confidence cell).

### Reading B — the edge is reading, and the boundary tests are calendar

- **Qwen3 reads worse than Phi-4** (61.8% vs 67.8% tape) despite being
  bigger, later-cutoff and heavily web-trained. Memorisation predicts the
  opposite sign.
- Holding calendar fixed and varying only corpus status: **DiD −3.8pp, se
  5.0, z=−0.77.** Wrong sign, not significant.
- Opus 5 falls 75.7% → 59.3% across a boundary it **never crossed** (both
  windows in-corpus).
- Opus's drift accuracy by year runs **backwards** for a recall story:
  2016 47.2%, 2018 54.7%, 2020 61.8%, 2022 55.4%, **2024 62.6%**, 2025
  57.5%, 2026 44.6%. 2016 is the most-memorisable year and nearly the worst.
- The in/out drift gap is +7.4pp for **Phi-4** and +12.7pp for **Qwen3** —
  models with validated zero recall. Whatever produces that gap is not
  corpus.

**Neither reading can be dismissed on this data.** Both are supported by
statistically solid results. They disagree because every clean test is
starved: Opus 5 has 37 post-cutoff events in the panel.

---

## Mistakes made today — do not repeat these

1. **`TaskStop` does not kill a process tree.** A driver kept running
   alongside its replacement; both read `_completed_ids()` at their own
   start and appended to the same file. 2,609 rows for 1,815 events, 794
   duplicates, silent. Fixed by `_RunLock` and a reporting dedupe guard.
   Kill by PID via `Get-CimInstance Win32_Process`, then verify zero
   survivors.
2. **Compared `notext` against `named` on different event subsets** and
   concluded memory beat reading. On identical events they are 82.7% vs
   83.2% — the arm simply self-selects easier events. Always run the
   like-for-like control before reporting a cross-arm comparison.
3. **Built a post-cutoff test with no event fixed effects** and got +11.4pp
   of "contamination" that was the calendar. The memory note
   `llm-contamination-test-design` warns about exactly this. Trust the
   existing `research_out_of_training.py` verdict of *inconclusive* over any
   fresh cut of the same data.
4. **Degraded accuracy by flipping only tape-correct calls**, which left the
   model's informative contrarian calls intact and produced a Sharpe of 1.16
   at "50% accuracy". A pure-random control (Sharpe −0.20 / +0.05 / −0.27)
   caught it. Always run the null through the same pipeline.
5. **Conflated tape agreement with drift prediction** for most of the day.

Two proposed purchases were also wrong and got caught before spending: Opus
5 post-cutoff verdicts (36 of 37 already bought) and, by extension, any
"buy more post-cutoff data" plan. The panel ends 2026-07-30.

---

## What to do going forward

### 1. Forward test — the only thing that settles it

Paper-trade the flagship on releases after 2026-05. Exact spec:

- **Signal**: Opus 5 `named` arm, `HARDENED_SYSTEM` + `task_named`,
  `SURPRISE_SCHEMA`, medium effort, adaptive thinking, consensus "unknown".
- **Universe**: |reaction| ≥ 5%, prior-session dollar volume ≥ $50M, top 5
  reporters per announce day.
- **Entry**: reaction price, 15 min after that event's own EDGAR acceptance.
- **Side**: FULL POLICY — with the tape if the verdict agrees, **against it
  otherwise, and neutral counts as otherwise**.
- **Exit**: T+3 if guidance moved, else T+1, at the session close.
- **Slots**: 6. **Costs**: budget 23.2bp round trip.
- **Do NOT include the 10% profit target.** It is the best of 171 rules
  swept on 2026-08-07 and has never been out-of-sample. Log what it would
  have done; do not trade it.

**Track drift hit rate, not P&L and not tape agreement.** Tape agreement
will look healthy regardless — it did for Phi and Qwen, both of which had
zero drift edge. Power: ~200 non-neutral verdicts to separate 57% from 50%
at z=2, which is about a year, or a preliminary read after two or three
earnings seasons.

### 2. Walk-forward the config selection

Untouched all day and probably the second-largest deflator. `shipped` 0.63
against `best` 1.70 is a 1.07 Sharpe swing from choosing a horizon. Pick the
config on data through year *T*, trade *T+1*, roll. Costs only compute.

### 3. Two prompt inputs worth adding (as a NEW study, not this one)

Any prompt change invalidates all 2,600+ cached Opus verdicts for
comparison, so this cannot be folded into the contamination work.

- **Real consensus EPS/revenue.** 27.5% of events currently return
  `not_stated` for `eps_vs_consensus` — the schema asks the model to compare
  against a consensus it was never given. Those events run 69.9% tape
  accuracy against 74.5% for `beat`.
- **Pre-print implied move.** Nothing in the prompt captures "was the beat
  big enough versus what was priced in," which is what sets the reaction
  sign. Needs point-in-time options data; using current chains for
  historical dates is a lookahead bug.

### 4. Do not build a confidence filter

Opus's high-confidence `named` verdicts are 94.6% accurate, **71.1% of them
are events the memory probe recalls**, and the high-confidence memory cell is
85.4%. It is the most attractive-looking filter in the study and the most
likely to evaporate live.

### 5. Housekeeping

- Qwen3's `max_tokens` reservation should drop from 2048 to 1024. Its
  tokenizer produces ~5,030 tokens where Phi produces ~3,300 for the same
  12k-char release, and the densest filings breach the 6,656-token slot.
  Two events lost to this (`HPE_20251015`, both arms; both recovered).
- 10 panel events have zero-byte text files, so the reading arms cover 1,805
  of 1,815. Memory probes cover all 1,815.
- `research_out_of_training.py` `COHORT` is pinned and must stay pinned.
- A $35 backfill of Opus 4.6 across the full panel would cut its in/out gap
  standard error from 10.3 to ~4. Worth it only if the forward test is
  ambiguous.

---

## What the paper should say

§5.7 currently frames `notext` as bounding memorisation. It does not, on
refusal-tuned models, and the free-response version should be labelled a
disclosure measure with `notextf` as the recall measure.

The honest claim the evidence supports:

> Memorisation is real and measurable — the study model predicts the next
> session's direction at 57.1% (z=+6.03) from ticker and date alone, and at
> 85.4% on the events it flags high-confidence. A memory-only strategy
> backtests at Sharpe 1.71. Whether the reading channel carries independent
> edge is **not determined** by this panel: the document adds nothing to
> drift prediction over recall, while corpus depth and a calendar-fixed
> cross-model test both point the other way. Resolution requires
> out-of-corpus data the panel does not contain.

That is weaker than the paper currently implies and stronger than "the edge
is fake." It is where the evidence actually sits.
