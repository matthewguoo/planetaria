# Minute-tape cross-sectional baseline — 20260811_0032 (SMOKE (first 8 months only — plumbing check, NOT results))

350,424 samples, 167 sessions, 153 names, grid 10:00-15:00 every 15m, target fwd-30m demeaned. Model: HGB regressor, one config, no sweep. LS = decile top-minus-bottom half-spread-deployed; costs charged per SIDE x2 (entry+exit crossings).

## Real model (walk-forward)

### prediction = HGB

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2022 | 1,785 | +0.0013 | +0.3 | +0.0 | -6.0 | -12.0 | -20.0 |

## Placebo (train labels shuffled within day, same pipeline)

### p_placebo0

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2022 | 1,785 | -0.0040 | -1.2 | +0.2 | -5.8 | -11.8 | -19.8 |

### p_placebo1

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2022 | 1,785 | -0.0014 | -0.4 | +0.2 | -5.8 | -11.8 | -19.8 |

### p_placebo2

| year | cross-sections | mean rank IC | IC t | LS gross bp/30m | net@3/side | net@6/side | net@10/side |
|---|---|---|---|---|---|---|---|
| 2022 | 1,785 | -0.0035 | -1.0 | -0.2 | -6.2 | -12.2 | -20.2 |

_Provenance: `research_mtape_baseline.py score --smoke` at ab5e6b8, 2026-08-11 00:33 ET_
