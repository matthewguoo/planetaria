# Is the entry-gate model overfit? Four fingerprints — 2026-08-10

Diagnostics on the wick study's HistGB entry gate (accept+60m, P>=0.45),
all walk-forward, all from cache:

1. TRAIN vs TEST AUC per step: train 0.90-0.99 vs test 0.69-0.82
   (gap +0.14-0.26, shrinking as the training window grows). The model
   memorizes its training set heavily AND generalizes anyway — the OOS
   numbers already price this in; nothing reported was in-sample.
2. RETRACE-ONLY model (the one pre-named economic feature): +41.6bp
   (t 1.66) of the full gate's +81.2 (t 3.33). Half the edge stands on
   the interpretable mechanism; the other half is 14-feature ML juice
   and carries the residual family risk.
3. THRESHOLD-FREE rank-IC of prob vs realized return, by year: -0.07 to
   +0.15, ~0.00-0.03 in 2024-2026. The model is a coarse BINARY screen
   (cut the doomed cohort), not an alpha ranker — its fine-grained
   signal content is near zero recently. Its most trustworthy property
   stays the defensive one: 2022 same-events -95bp ungated vs -11 gated.
4. SEEDS 7/17/27: byte-identical results. No seed fragility.

VERDICT: not overfit in the reporting sense (walk-forward honest, no
leakage fingerprint, no seed luck); substantially overfit in the fitting
sense (train AUC ~0.95), which the OOS already absorbed; forward
expectation honestly bracketed [retrace-only floor +40bp, full gate
+81bp], with the threshold-family and single-panel caveats standing.
When the delayed-entry strategy is built, journal BOTH arms (ungated +
gated) so live data adjudicates the bracket. These four diagnostics are
the template for any future gate (gff fade-quality, day2 continuation).

_Provenance: ad-hoc over research_wickout machinery, walk-forward
identical to research_delayed_account.py; end of 2026-08-10 session._
