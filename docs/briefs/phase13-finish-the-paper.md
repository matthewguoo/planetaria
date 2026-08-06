# Phase 13 brief: finish and publish the paper

Paste this as the opening prompt of a fresh Claude Code session in
`C:\Users\matth\Desktop\planetaria`. Most of the work is done and committed.
What remains is verification, publication, and two decisions that are
Matthew's.

## Where things stand

Phase 12 shipped: the lookahead audit, the gate mutations, the LAB curve, and
the paper. Then three more jobs landed on top — acceptance-relative panels, the
two contamination arms, and a ten-year extension — plus two full restyles.

**All LLM inference is complete. Do not submit another batch.** Measured spend
is **$72.67** against a $77.17 ceiling ($37.17 from phase 12 plus $40 Matthew
authorised). Cached verdicts:

| arm | effort | n | what it is |
|---|---|---|---|
| named | medium | 2,626 | the live prompt |
| named | low | 180 | effort calibration slice |
| blind | medium | 437 | identity ablation |
| notext | medium | 440 | memory probe |

The batches were pathologically slow — roughly ten hours for work that took
3.9 minutes earlier the same day — but they completed. If a future batch looks
stalled, note that `request_counts.succeeded` **does not update live**; it
flushes at the end. A live zero means nothing. One batch was cancelled on that
misreading and lost ~400 requests' worth of queue position for nothing.

### The panel rebuild that changes every number

`research_event_panel.py` replaced the fixed `[16:00, 16:20]` ET reaction
window with one keyed to each release's own EDGAR acceptance. Both panels are
still reachable and the paper reports the difference:

| panel | events | gated bp | spread |
|---|---|---|---|
| v1, all events (pre-audit) | 1,537 | +167.4 | +363.1 |
| v1, mis-timed dropped | 1,312 | +171.7 | +393.7 |
| v2, acceptance-relative | 1,815 | see below | |

v2 is the default everywhere (`--panel v1` restores the old behaviour). It
spans **2016-01-13 to 2026-07-30**, so the study is now a decade, and
2016-01→2021-07 is a genuine holdout: no design decision ever saw it.

### Files that matter

| path | what |
|---|---|
| `backend/scripts/research_event_panel.py` | v2 panels: ET calendars, acceptance-relative reactions, per-event paths |
| `backend/scripts/research_llm_contamination.py` | arms, prompts, budget guard, `compute_arms()` |
| `backend/scripts/research_holding_period.py` | exits, horizons, mutations, account model, trade export |
| `backend/scripts/build_report_data.py` | every number the paper cites → `docs/report_data.json` |
| `backend/scripts/build_paper.py` | renders `docs/report.html`; `--print` writes `report_print.html` |
| `backend/scripts/finalize_paper.sh` | the whole chain, collect → PDF, refuses if the appendix under-prints |
| `docs/report_template.html` | prose, arXiv styling, all render code |
| `backend/scripts/verify_no_lookahead.py` | seven mechanical checks, 6 pass |

## What to do first

Run the chain and read what it prints:

```bash
cd backend && bash scripts/finalize_paper.sh
```

It collects, fetches any missing paths, scores the arms, re-runs
best/mutations/trades on v2, rebuilds `report_data.json`, renders both HTML
builds and exports the PDF. It exits non-zero if the appendix prints fewer
than 100 rows, which is the failure mode that already bit once.

Then verify in a browser before publishing — the page is generated, so a
render bug is invisible in the source:

- no `undefined` / `NaN` / `[object Object]` anywhere
- every table populated, figures 1-6 drawing
- both themes repaint the SVGs (they read CSS variables at draw time)
- the appendix sorts, filters, pages and resets

## Job 1 — publish

`Artifact` with `docs/report.html`, and **pass the existing URL** so it updates
in place rather than minting a new one:

```
https://claude.ai/code/artifact/98237b3e-5ded-42f3-8f9d-12a3d7041d0a
```

Favicon stays 📉. Then send Matthew the PDF from the scratchpad with
`SendUserFile`.

## Job 2 — check the arms actually removed the limitation

This is the thing Matthew asked about most. Section 7's first bullet used to
read "two contamination arms were designed and not run". It is data-driven now
and rewrites itself when the arms are present. Confirm on the rendered page
that it describes what the arms found rather than their absence, and that
§4.3 says they ran. The mechanism was verified with a synthetic payload
(commit `91afbad`) but never with real data.

Read the arms result critically before believing it:

- `blind` still names the right issuer some fraction of the time — that rate is
  reported, and a null result there means "identity did not matter" only to the
  extent the scrub worked.
- `notext` is the upper bound on memorisation. Section 5.7 already carries an
  observational bound from the age gradient: **at most 29% of the gated edge**,
  95% CI, with the point estimate the wrong sign for memorisation.
- Arms under 100 events are excluded as under-covered. This exists because 23
  surviving verdicts once produced "memorisation buys +1859bp" off five coin
  flips.

## Job 3 — two decisions that are Matthew's

**The engine still runs the weakest configuration in the paper.** The shipped
5% stop / 2× target compounds to roughly +22% over the span against +230% for
the search winner and +209% for plain T+1, and under SPY's +88%. Changing
`earnings_reaction.py` needs his call.

**The gate throws away its best signal.** `neutral` verdicts are never traded;
taken *against* the tape they were the strongest fade in the study. "Never
stand down" — with the tape when the verdict agrees, against it otherwise —
beat the shipped gate on every axis measured. Also his call.

## Standing rules

Commit locally per step, NEVER push. Paper-lock stays. Keys only in `.env`.
Research stays in `scripts/research_*` + `docs/notes`; `app/` must never learn
to read research artefacts. 407 backend and 139 frontend tests stay green;
ruff clean.

### Traps this phase already fell into

- `build_paper.py` filters the payload through an **allowlist**. A new key in
  `build_report_data.py` that is not listed is silently dropped, and the page
  renders its "not computed yet" guard — indistinguishable from missing data.
- `const` is not hoisted. A `PRINT` reference above its declaration killed the
  page script and emptied the 1,312-row appendix while everything above it
  still rendered fine.
- A `file://` query string does not survive every viewer. Print mode is a flag
  baked into the payload for that reason.
- Prose goes stale faster than numbers. Spans, regime counts, control counts
  and the gate/cost parameters are all derived now; keep it that way.
- The budget guard counts submitted-but-uncollected batches at estimate.
  `_measured()` is the banked figure; `_spent_so_far()` is the one to refuse
  on. Do not print the second under a "measured" label.
