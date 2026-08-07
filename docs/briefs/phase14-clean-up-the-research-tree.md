# Phase 14 — clean up the research tree

Written 2026-08-06, at the end of the session that finished the paper. The
study is done; what follows is the debt it left. Nothing here changes a
number in the paper, and the first task is the check that proves it.

> **Status, 2026-08-06 (commit 47af9d5).** The tree also *moved*: the study
> now lives in `research/pead-llm-gate/`, not `backend/scripts/`, so every
> path below is relative to that. **Done:** §0 the regression gate (payload
> and rendered paper both rebuild byte-identical), §1 archive what nothing
> imports (`scripts/archive/`), §5 the notes prune (62 superseded logs to
> `notes/archive/`; the runbook and the SIP preflight moved to `docs/` as
> operational). Filesystem anchors are now centralised in `scripts/_paths.py`
> — which is §2's problem in miniature, solved for paths but not for the
> module cycle. **Still open:** §2 the `research_holding_period` ↔
> `research_llm_contamination` import cycle, §3 the cohort audit, §4 the
> 97 KB module split, §6 the duplicate bars in `cache/`, §7 folding the new
> one-offs into the builder. §8 is not cleanup and is restated in
> `research/README.md`.

## Standing constraints (unchanged)

- Commit locally per step, **never push**.
- Paper-lock stays: `docs/report.html` is generated, never hand-edited.
- Keys only in `.env`.
- Research stays in `scripts/research_*` + `docs/notes`. **`app/` must never
  learn to read a research artefact.** Two comments in `app/` currently name
  research modules (`app/services/sim_account.py:29`,
  `app/strategies/earnings_reaction.py:111`) — those are citations in prose
  and are fine; an import or a file read would not be.
- 407 backend and 139 frontend tests stay green; ruff clean.

## 0. The regression gate, before touching anything

Everything below is a refactor, so the only thing that matters is that the
paper's numbers do not move. Record the baseline first:

```bash
cd backend && ./.venv/Scripts/python.exe scripts/build_report_data.py && cp ../docs/report_data.json /tmp/baseline.json
```

After each step, rebuild and diff. Any change to a key the paper renders is a
bug in the refactor, not an improvement. This session lost an hour to exactly
this class of failure twice: registering Haiku in the shared `MODELS` registry
silently turned Section 5.9 into a five-model test (bound 85.9 → 69.6bp), and
omitting a key from `build_paper.KEEP` rendered "not computed" over a fully
computed appendix.

## 1. Archive what nothing imports

Three modules are reachable from no other file and from no builder:

| module | what it was |
|---|---|
| `research_bracket_sweep.py` | superseded by the joint search in `research_holding_period.stage_best` |
| `research_ict_backtest.py` | an unrelated strategy, never cited by the paper |
| `research_pead_account_sim.py` | superseded by `account()` / `account_slots()` |

Move to `scripts/archive/` rather than delete — they are the provenance for
notes still in `docs/notes/`. Confirm with a build + diff that the payload is
byte-identical.

`research_llm_ab.py`, `research_leadup_backtest.py`,
`research_pead_backtest.py` and `research_leadup_account_sim.py` are cited by
notes or imported transitively (`research_event_panel` imports
`research_leadup_account_sim`) — check each before moving.

## 2. Break the import cycle

`research_holding_period` and `research_llm_contamination` import each other.
It works only because the names each needs are defined before first use, which
is a property of the current line ordering rather than of the design. Adding an
import at the top of either file can break both.

Extract the shared primitives — `CACHE`, `GATE`, `TOP_PER_DAY`, `MIN_DV`,
`COSTS_BP`, `_bars_for`, `_spy_daily`, `load_universe*`, `paths_file` — into
`scripts/research_common.py` and have both import from it. This is the single
highest-value item on the list.

## 3. Make the cohort pattern explicit everywhere

`research_out_of_training.COHORT` now pins which models Section 5.9 tests,
after the Haiku leak. The same hazard exists anywhere a study reads "all of"
a shared registry. Audit for it, and prefer an explicit tuple over a live
registry read in any function whose output the paper cites.

## 4. `research_llm_contamination.py` is 97 KB

It holds the model registry, prompts, schemas, the batch client, cost
estimation, the scoring pipeline, the arms study and the curve builder. Split
along the seams that already exist:

- `contamination/registry.py` — `MODELS`, `TAG_OF`, `CUTOFF_OF`, `PRICE_OF`,
  `LEGACY_THINKING`, `request_key`, `_model_of_id`
- `contamination/prompts.py` — systems, schemas, `anonymise`, task builders
- `contamination/batch.py` — `client`, `build_request`, `estimate_cost`,
  `_spent_so_far`, the submit/collect/watch stages
- `contamination/scoring.py` — `_load_results`, `compute_arms`, the curve

Keep `_load_results`' one-model default. It is the containment, not a
convenience, and the docstring says so.

## 5. `docs/notes/` — 90 files, most superseded

Timestamped run logs: 20 `best_config_*`, 14 `gate_mutations_*`, 14
`contamination_arms_*`, 10 `out_of_training_*`. Keep the newest of each family
plus anything the paper cites by name; move the rest to
`docs/notes/archive/`. `runbook-first-earnings-night.md` is operational, not
research — move it to `docs/` proper.

## 6. `scripts/_leadup_cache/` is 173 MB

Several `bars_ohlc_*` parquets overlap or were superseded by a wider fetch
(e.g. `2021-07-11_2022-01-03_455` alongside `..._1000`). `_bars_for()` selects
by covering range, so narrower duplicates are dead weight. Enumerate what
`_bars_for` can actually select across the panel's date range, and drop the
rest. Do not touch `texts/` (2,844 cached filings, ~$100 of EDGAR fetches and
the input to every verdict) or `llm_contam/` (the verdicts themselves,
$118.09 of inference — **these are irreplaceable**, several models that
produced them have since been retired).

## 7. Fold the new one-offs into the pipeline or archive them

Written today, each answering one question:

- `research_conditional_exit.py` — the twelve-rule exit sweep. Its finding is
  now the flagship; the sweep itself is not in the payload. Either add a
  `compute()` to the builder or archive it with a note.
- `research_slots.py` — the capacity sweep. `capacity()` in
  `build_report_data` re-implements a slice of it against
  `account_slots()`. One of the two should go.
- `research_legacy_did.py` — feeds Appendix B, keep.
- `research_spread_model.py` / `research_spread_sim.py` — feed Section 6, keep.

## 8. Known-open research questions (not cleanup)

- The memorisation bound is supply-constrained at ±86bp: every pre-2025-cutoff
  model now 404s. It cannot be narrowed by spending.
- The conditional exit and the six-position limit were both selected after
  seeing results. A pre-registered test on genuinely forward data is worth
  more than any further sweep.
- 2026 is the only year with a negative selection spread (−30.3bp, t = −0.20
  on 68 gated trades). Too small to update on in either direction; it will
  answer itself with another two quarters of events.
