# Phase 12 brief: validate the LLM-gate study, extend it, and write the paper

Paste this file as the opening prompt of a fresh Claude Code session in
`C:\Users\matth\Desktop\planetaria`. Four jobs, in order — (1) prove the
backtest had no lookahead, (2) test more gate mutations, (3) finish the LAB
curve, (4) write the paper. Ask Matthew only for money/scope decisions.

## Where things stand

A five-year study of the LLM gate on after-close earnings reactions is
complete and committed (`a2dbea2` and the eight commits before it). Opus 5,
medium effort, Batch API, **$37.17 measured, no further LLM spend needed** —
1,732 verdicts are cached in
`backend/scripts/_leadup_cache/llm_contam/results_named.jsonl` and every
stage below reads them off disk. **Do not re-run inference.**

Headline, on corrected next-session exits, 1,537 scored events / 1,064 gated:

| hold | gated | 2021-23 | 2024-26 | vetoed | spread |
|---|---|---|---|---|---|
| T+1 | +167.4 | +143.5 | +186.6 | -195.6 | +363.1 |
| T+2 | +179.4 | +140.4 | +210.7 | -235.4 | +414.8 |
| T+3 | +175.3 | +135.2 | +207.5 | -277.3 | +452.7 |

Best configuration, chosen on 2021-23 and scored on 2024-26: **T+1, extended
to T+3 when guidance moved, no stop, 20% take-profit** (train +161.2, test
+186.9). Account at 30% average deployed capital: +270.7% total, 30.0% CAGR,
5.1% max drawdown, Sharpe 2.98, alpha +26.4%/yr, beta 0.021 vs SPY. The
SHIPPED bracket (5% stop / 2x) returns **-16.2bp per trade out of sample**.

The value is refusal, not selection: taking every gated-5% reaction earns
+48.8bp; the model's selection adds +137bp and its refusal adds +306bp. The
vetoed leg gets monotonically worse with horizon while the gated leg is flat.

Two bugs were found and fixed during the study; both are in commit messages
and in code comments. Read them before trusting any older number:
- `research_pead_backtest.py` exited on the LAST session in a 4-day fetch
  window, not the next one, so every panel cached before 2026-08-06 measures
  a 2-4 session hold. Fixed; older docs in `docs/notes/` are stale.
- `anonymise()` in `research_llm_contamination.py` had an unbounded
  `^EX-99[^\n]*` strip that deleted whole documents (`strip_html` returns one
  line). Caught by the `scrub` audit stage before any spend.

### Files that matter

| path | what |
|---|---|
| `backend/scripts/research_llm_contamination.py` | the study: arms, contamination controls, scoring, curves |
| `backend/scripts/research_holding_period.py` | corrected exits, horizon sweep, joint best-config search, trade export |
| `backend/scripts/research_bracket_sweep.py` | bracket surface over cached price paths |
| `backend/scripts/build_report_data.py` | consolidates every number into `docs/report_data.json` |
| `_leadup_cache/event_paths_multi.parquet` | per-event multi-session paths + excursion first-touch grid (1,537) |
| `_leadup_cache/llm_contam/results_named.jsonl` | the cached verdicts. Never regenerate. |
| `frontend/src/lab/` | the LAB deck (separate Vite entry at `/lab.html`) |
| `frontend/public/study-*.json` | static exports the deck reads; gitignored |

Engine: `earn-night` (live) and `earn-shadow` (shadow) both enabled,
min_move_pct 5.0, short_run5d_floor -5.0, max_spread_pct 2.0, effort medium.
**Not yet changed to the study's best config** — that needs Matthew's call.
SIP is still not entitled (`scripts/verify_sip_entitlement.py` returns 403),
so no after-hours entry can fill live yet.

---

## Job 1 — prove there is no lookahead

This is the load-bearing claim of the whole study and it has NOT been
audited end to end. The assertion to verify: **every input the model saw was
available at 16:20 ET on the announce day, and nothing derived from the
outcome reached the prompt, the gate, or the selection.**

Write `backend/scripts/verify_no_lookahead.py` that checks each of these
mechanically and prints PASS/FAIL per item — do not just read the code and
conclude it looks right.

1. **Prompt contents.** Rebuild the exact request for a sample of ~30 events
   via `build_request()` and assert the payload contains no `exit`,
   `fwd_bp`, `move_pct`, or any price after the reaction print. The tape
   reaction is deliberately absent — the model must not see the move it is
   gating. Assert it.
2. **Release text provenance.** `fetch_release_text` matches on
   `filingDate == day` with item 2.02. Verify none of the cached texts came
   from an 8-K/A amendment filed later, and that accession-scoped EDGAR URLs
   are immutable (they are, but prove it for two events by refetching).
3. **Run-up feature.** `load_universe`'s `ctx()` uses `c[i-1]/c[i-6]` and
   `dv[i-1]`. Assert index `i` is the announce day and that no element at or
   after `i` enters either feature.
4. **Universe selection.** `build_universe_window` ranks on the window's
   FIRST sessions, not the whole span — verify. Then quantify the residual
   survivorship: names in the EDGAR calendar that are absent from today's
   `company_tickers.json`. Report the count; it cannot be fixed, only stated.
5. **No retrieval.** Grep the submitted batch payloads for a `tools` key.
   There should be none. Confirm the schema-constrained output path cannot
   invoke web search.
6. **Entry price realism.** `react` is the last AH print within 15 minutes of
   16:05. Confirm the timestamp of that print is <= 16:20 for every event.
7. **Manual spot-check.** Read five cached release texts end to end and
   confirm none contains post-release market commentary (a press release
   should not, but a mis-parsed exhibit might have pulled in a later 8-K).

If any check fails, the finding is more important than the result. Report it
the way the two bugs above were reported: what broke, what it changes, and
what the corrected number is.

## Job 2 — more gate mutations

The current gate is: trade when `verdict.direction` agrees with `sign(tape
reaction)` and `|reaction| >= 5%`. Everything else is a no-trade. That throws
away information — a confident disagreement is a signal, not an absence of
one. Add these as scoreable mutations in `research_holding_period.py`
(the resolver and the account model already take arbitrary per-event side
and horizon vectors):

- **Strong reject → fade.** When the model is bearish with high confidence
  and the tape is up (or the mirror), take the OPPOSITE side rather than
  standing down. This is the most interesting one: the study says the vetoed
  bucket returns -196 to -283bp, so the refusals contain a tradeable signal
  and currently it is only used to avoid a loss, not to make a gain.
- **Confidence-graded reject.** Same as above but only on `high`, then only
  on `high|medium`, to see where fading stops working.
- **Pure LLM direction.** Ignore the tape entirely; trade the verdict.
  Isolates how much of the edge is the model versus the agreement rule.
- **Pure tape.** The mechanical baseline, already computed (+48.8bp) — keep
  it in the same table for comparison.
- **Neutral verdicts.** Currently never traded. Score them as a bucket: is
  `neutral` genuinely uninformative, or is it a weak directional signal?
- **Fundamental agreement.** Require `eps_vs_consensus` and
  `revenue_vs_consensus` to agree with `direction`, and separately require
  `guidance` to agree. Which of the four schema fields carries the edge?
- **Quality flags as a veto vs a size multiplier** — the live code already
  shrinks size by 0.75x on any flag; test flags as a hard veto and as an
  inverter.

Score every mutation on 2021-23 / 2024-26 split, with the account model, and
put them in one table. Expect most to fail; report them anyway. The mutation
that would change the strategy most is the fade, so give it the most
scrutiny — particularly whether it survives costs, since fading a 5%+ move
means crossing a wide after-hours spread in the direction nobody wants.

## Job 3 — finish the LAB curve

`frontend/public/study-curve.json` currently carries the top-3 configs plus
SPY. Add two series so the deck shows the full comparison the paper makes:

- **`shipped`** — T+1 with the 5% stop / 2x target, the live configuration.
  It is already in `picks` but gets overwritten when it lands outside the
  top 3; make it unconditional.
- **`t1plain`** — T+1 unconditional exit, no bracket at all. The honest
  "do nothing clever" baseline.

`BenchmarkChart.tsx` reads `labels`/`series`/`stats` generically, so adding
keys to `SERIES` (fixed order, fixed hue — never cycle) is the only frontend
change. Validate any new colour with the dataviz palette validator before
using it; the current trio passes in both modes.

## Job 4 — write the paper

A draft exists at
`%LOCALAPPDATA%\Temp\claude\C--Users-matth-Desktop-planetaria\85dee7a1-04de-4399-8b2a-f2cc6a329dd1\scratchpad\report.html`
— structure, design system and reference list are good, **every number in it
is pre-correction and must be rebuilt.** Regenerate `docs/report_data.json`
from `build_report_data.py` (update it to read corrected exits first), then
rewrite the results sections against it. Publish with the Artifact tool.

Required content, in this order:

1. **Abstract** — the corrected headline, the refusal decomposition, the
   bracket finding, and the contamination status in five sentences.
2. **Exact strategy specification** — the production config as a table, read
   out of `earnings_reaction.py`, not paraphrased.
3. **Data and universe** — the selection cascade (1,787 AMC reporters/yr ->
   416 past the 5% gate -> 354 in the live watchlist shape -> 312 scored),
   and the point that the study covers 88% of what the live strategy sees.
4. **Methodology** — one request per event, no shared context, no tools; the
   three arms; the contamination controls; the lookahead audit from Job 1.
5. **Results** — per-year, per-liquidity-quintile, horizon sweep, bracket
   surface, mutations from Job 2, account curves vs SPY/QQQ/TQQQ with the
   iso-return leverage control.
6. **Limitations** — unrun `blind`/`notext` arms, survivorship, flat 13bp
   costs, the unproven after-hours fill, one bear market, paper fills.
7. **References** — keep the existing list; it is real published work. Do not
   add citations you cannot verify.
8. **Appendix: trade decisions.** A compact table of every scored event —
   date, ticker, verdict, confidence, guidance, reaction, taken/declined,
   reason, and P&L under the best config. 1,537 rows; render it paginated or
   virtualised, sortable, with a filter row, from `study-trades.json`. This
   is the appendix Matthew asked for and it is the paper's evidence base —
   every claim in the results should be checkable against a row in it.

Style: academic working paper. The design plan in the draft (mono display,
Charter body, amber accent, both themes) is settled — keep it. Load
`artifact-design` before editing the page and `dataviz` before touching a
chart; both were used for the draft.

## Standing rules

Commit locally per step, NEVER push. Paper-lock stays. Keys only in `.env`
(`CLAUDE_API_KEY` is the Anthropic key; the research scripts read either
name, and `app/` must never depend on the API). Research stays in
`scripts/research_*` + `docs/notes` — `app/` must not learn to read research
artefacts; the LAB deck reads static JSON from `frontend/public/` for exactly
this reason. Broker/feed behaviour verified empirically via `scripts/verify_*`
before code depends on it, with dated findings in comments. 407 backend and
139 frontend tests stay green; ruff clean.
