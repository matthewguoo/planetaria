# gff gate robustness: placebo, seeds, ablation — 20260810_2359

Primary readout everywhere: HGB, both legs, tau=0.50, net@10 lift vs ungated on the registered selection, walk-forward 2019-26. Reference from the gate note: +8.8bp lift at 61% keep.

reference rerun: lift +8.8bp at 61% keep (t gated +4.37)

## Placebo: training labels shuffled within year (5 draws)

If any draw shows lift like the real one, the pipeline manufactures edge.

| draw | keep% | lift bp |
|---|---|---|
| 1 | 66 | +1.0 |
| 2 | 60 | -3.5 |
| 3 | 64 | +2.6 |
| 4 | 66 | +1.9 |
| 5 | 63 | +1.4 |

placebo mean +0.7bp (real: +8.8)

## Seed sensitivity

| seed | keep% | lift bp | t gated |
|---|---|---|---|
| 20260810 | 61 | +8.8 | +4.37 |
| 1 | 61 | +8.8 | +4.37 |
| 7 | 61 | +8.8 | +4.37 |
| 42 | 61 | +8.8 | +4.37 |
| 777 | 61 | +8.8 | +4.37 |

## Ablation (drop the model's favorite features)

| features | keep% | lift bp | t gated |
|---|---|---|---|
| all 23 (reference) | 61 | +8.8 | +4.37 |
| - n_fades_today | 63 | +4.2 | +3.44 |
| - n_fades_today, ret1 | 63 | +7.5 | +4.07 |
| - n_fades_today, ret1, turn_bp | 63 | +1.5 | +2.88 |
| - top5 (also pm_dollar_log, spy_rv20) | 66 | +1.5 | +2.97 |

_Provenance: `research_gff_gate_robustness.py` at ec08630, 2026-08-11 00:00 ET_
