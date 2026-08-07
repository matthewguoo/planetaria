# research/

Studies. Code that produces evidence, kept out of `backend/` so that the
dependency runs one way only: **research reads the app; the app never reads
research.** A study importing `app.config` is normal. An `app/` module
importing a research artefact — a parquet, a note, a payload — is a bug, and
the reason the trees are separate rather than adjacent.

| | |
|---|---|
| `pead-llm-gate/` | Working Note PR-2026-01 — gating post-earnings drift with an LLM. Finished 2026-08-06. |
| `notes/` | Research that belongs to no single study (overnight alpha, the latency band). |

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

**The payload is the regression gate.** Every change in this tree is a
refactor whose only success criterion is that the numbers do not move:
snapshot `docs/report_data.json`, make the change, rebuild, and diff. A
byte-identical payload is the pass. Anything else is a bug in the refactor,
not an improvement.

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
