# research/

Studies. Code that produces evidence, kept out of `backend/` so that the
dependency runs one way only: **research reads the app; the app never reads
research.** A study importing `app.config` is normal. An `app/` module
importing a research artefact — a parquet, a note, a payload — is a bug, and
the reason the trees are separate rather than adjacent.

| | |
|---|---|
| `pead-llm-gate/` | Working Note PR-2026-01 — gating post-earnings drift with an LLM. Finished 2026-08-06. |
| `chart-llm-gate/` | Can a model read a chart? Five retail intraday setups, anonymised candles, local weights. Started 2026-08-07. |
| `tug-of-war/` | Night vs day returns by retailness decile. Premium confirmed, trade dominated. 2026-08-10. |
| `astro-null/` | The planetaria null battery: 17 calendar/astro partitions on SPY, 1993-2026. All null. 2026-08-10. |
| `intraday-mft/` | GHLZ first→last half-hour momentum, post-publication. Dead. 2026-08-10. |
| `notes/` | Research that belongs to no single study (overnight alpha, the latency band, the 2026-08-10 alpha scan). |

The two LLM studies are deliberately built on the same statistic — a gate
spread with a within-block permutation null — so they can be read against each
other. They differ in what they can prove: `pead-llm-gate` had to *bound*
memorisation and got stuck at ±86bp because named, dated earnings releases are
extensively written about; `chart-llm-gate` *dissolves* the problem by
stripping the identity off the input, because no corpus pairs a normalised
candle table with what happened next.

The paper itself is **not** here: `docs/report.html` is the published output
and stays where a reader expects it. This tree is the machinery that
generates it.

---

## pead-llm-gate

```
scripts/    the harness — one module per question, plus the two builders
  archive/  reachable from no builder; kept because notes still cite them
notes/      dated run logs, newest of each family
  archive/  superseded reruns
cache/      bars, filings, verdicts (gitignored, 173 MB)
```

### Rebuild the paper

```bash
cd research/pead-llm-gate && ../../backend/.venv/Scripts/python.exe scripts/build_report_data.py
```

then

```bash
cd research/pead-llm-gate && ../../backend/.venv/Scripts/python.exe scripts/build_paper.py
```

The first writes every number the paper cites to `docs/report_data.json`; the
second renders `docs/report.html` from `docs/report_template.html` plus that
payload. Nothing in the paper is transcribed by hand, so the paper cannot
drift from the harness — and `report.html` is generated, never hand-edited.

**The regression gate has two tiers, and the second is the real one.**

1. `docs/report_data.json` should rebuild byte-identical. It does — but only
   between stage runs. `curve_raw.updated` is a timestamp read out of
   `payload/study-curve.json`, so re-running a stage legitimately moves it,
   and `curve_bracketed` carries stage output the paper never renders.
2. **`docs/report.html` must rebuild byte-identical.** This is the invariant.
   It is built from an allowlist (`build_paper.KEEP`) plus the trade
   appendix, so it is immune to churn in fields nothing reads and it fails
   loudly on any change to a number the paper actually cites.

Diff tier 1 first because it localises a break; trust tier 2 to decide
whether a break matters.

### payload/ is an input, not an output

`payload/` is gitignored and looks disposable. It is not:
`build_report_data.py` READS `study-curve.json` and `study-curve-bracketed.json`
to populate `curve_raw` and `curve_bracketed`, and `build_paper.py` reads
`study-trades.json` for the appendix. Deleting them silently degrades the
payload — every headline number survives, the equity curves and the flagship
spec table vanish, and the paper renders its "not computed" guards instead.
That happened on 2026-08-06 when the two curve files were mistaken for
frontend assets.

Regenerate (no inference, just compute — the verdicts are cached):

```bash
cd research/pead-llm-gate && ../../backend/.venv/Scripts/python.exe scripts/research_holding_period.py best
```

and, for the bracketed curve,

```bash
cd research/pead-llm-gate && ../../backend/.venv/Scripts/python.exe scripts/research_llm_contamination.py curve --brackets --effort medium
```

`study-trades.json` comes from `research_holding_period.py trades`.

Two silent-failure modes have already cost an hour each, and neither raises:

- a study reading *all of* a shared model registry silently changed which
  models Section 5.9 tested when a new one was registered. Pin the cohort
  explicitly (`research_out_of_training.COHORT`) in any function the paper
  cites.
- a key missing from `build_paper.KEEP` renders "not computed" over a fully
  computed section. `KEEP` is an allowlist; anything new in
  `build_report_data` must be added to it in the same commit.

### cache/ is irreplaceable

Gitignored, so nothing but this paragraph protects it.

- `cache/texts/` — 2,844 EDGAR filings, roughly $100 of fetches, and the
  input to every verdict.
- `cache/llm_contam/` — the verdicts themselves: **$118.09 of inference**.
  Several of the models that produced them have since been retired, so this
  cannot be re-bought at any price.

Do not delete either to reclaim space. `cache/bars_ohlc_*` is the only part
that is safely regenerable, and some of it is genuinely redundant — narrower
date windows superseded by wider fetches that `_bars_for()` now selects
instead.

### Paths

`scripts/_paths.py` holds every filesystem anchor. Before the study moved out
of `backend/scripts/` each module derived its own from `Path(__file__)`, which
worked only because they all sat at the same depth; moving them broke that
assumption in fourteen files at once. Add anchors there, not inline.

### Known-open questions

Not cleanup — the things the study could not settle:

- The memorisation bound is supply-constrained at ±86bp: every model with a
  training cutoff before 2025 now 404s. It cannot be narrowed by spending.
- The conditional exit and the six-position limit were both selected after
  seeing results. A pre-registered test on genuinely forward data is worth
  more than any further sweep.
- 2026 is the only year with a negative selection spread (−30.3bp, t = −0.20
  on 68 gated trades). Too small to update on in either direction; another
  two quarters of events will answer it.
