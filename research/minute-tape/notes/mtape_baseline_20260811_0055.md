# Minute-tape cross-sectional baseline — 20260811_0055 (full panel)

2,413,957 samples, 1,150 sessions, 267 names, grid 10:00-15:00 every 15m, target fwd-30m demeaned. Model: HGB regressor, one config, no sweep. LS = decile top-minus-bottom half-spread-deployed; costs charged per SIDE x2 (entry+exit crossings).

## Real model (walk-forward)

### prediction = HGB

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2024 | 5,292 | +0.0077 | +3.5 | +0.4 | -5.6 | -11.6 | -19.6 |
| 2025 | 5,250 | +0.0106 | +4.8 | +0.5 | -5.5 | -11.5 | -19.5 |
| 2026 | 3,087 | -0.0025 | -0.8 | -0.5 | -6.5 | -12.5 | -20.5 |

## Placebo (train labels shuffled within day, same pipeline)

### p_placebo0

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2024 | 5,292 | -0.0045 | -2.7 | -0.3 | -6.3 | -12.3 | -20.3 |
| 2025 | 5,250 | +0.0015 | +0.9 | +0.2 | -5.8 | -11.8 | -19.8 |
| 2026 | 3,087 | +0.0019 | +0.7 | -0.1 | -6.1 | -12.1 | -20.1 |

### p_placebo1

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2024 | 5,292 | -0.0004 | -0.3 | -0.3 | -6.3 | -12.3 | -20.3 |
| 2025 | 5,250 | +0.0001 | +0.1 | +0.4 | -5.6 | -11.6 | -19.6 |
| 2026 | 3,087 | +0.0011 | +0.5 | +0.1 | -5.9 | -11.9 | -19.9 |

### p_placebo2

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2024 | 5,292 | -0.0030 | -1.7 | +0.1 | -5.9 | -11.9 | -19.9 |
| 2025 | 5,250 | -0.0032 | -1.8 | -0.1 | -6.1 | -12.1 | -20.1 |
| 2026 | 3,087 | -0.0020 | -0.7 | -0.1 | -6.1 | -12.1 | -20.1 |

_Provenance: `research_mtape_baseline.py score` at 7a4a213, 2026-08-11 01:00 ET_
