# Predictive content with the bracket removed — 2026-08-07

20,550 setups. Mean SIGNED forward return in bp — `side x (close[i+h]/close[i] - 1)` — gross of costs, truncated at the session close. No stop, no target: this is what the pattern knows, before any exit rule gets to destroy or preserve it.

| strategy | n | 3b | 6b | 12b | 24b | 48b | 78b |
|---|---|---|---|---|---|---|---|
| orb | 3,464 | -0.7 (-0.9) | -0.2 (-0.2) | +1.1 (+0.8) | -0.2 (-0.1) | -3.1 (-1.5) | -1.4 (-0.6) |
| vwap | 5,542 | -0.4 (-0.9) | +0.4 (+0.7) | +1.1 (+1.4) | +2.1 (+2.1) | +0.8 (+0.6) | +0.5 (+0.4) |
| ema | 6,865 | -0.5 (-1.4) | -0.1 (-0.3) | +0.6 (+0.9) | +1.8 (+2.1) | +0.9 (+0.9) | +0.5 (+0.4) |
| sweep | 1,728 | -0.9 (-0.9) | -0.7 (-0.4) | -0.4 (-0.2) | +1.8 (+0.8) | +2.1 (+0.8) | +2.9 (+1.0) |
| flag | 2,951 | +1.1 (+1.6) | +0.7 (+0.8) | +1.8 (+1.3) | +0.9 (+0.5) | -0.2 (-0.1) | -1.3 (-0.6) |
| ALL | 20,550 | -0.3 (-1.2) | +0.1 (+0.2) | +0.9 (+2.0) | +1.4 (+2.4) | +0.1 (+0.2) | +0.1 (+0.1) |

Cells are `mean bp (t-stat)`.

## The same statistic on random entries

Not zero, and that is the point: a random LONG in a year that drifted up makes money, so the strategies must be read against this row and not against zero. Random entries here take a random side at a random RTH bar in the same tape.

| arm | n | 3b | 6b | 12b | 24b | 48b | 78b |
|---|---|---|---|---|---|---|---|
| random | 3,996 | +0.5 (+0.9) | +0.3 (+0.5) | +1.1 (+1.2) | +0.1 (+0.1) | -0.4 (-0.3) | -0.7 (-0.4) |

## Edge over random, per strategy (bp)

| strategy | 3b | 6b | 12b | 24b | 48b | 78b |
|---|---|---|---|---|---|---|
| orb | -1.2 | -0.6 | -0.0 | -0.3 | -2.7 | -0.7 |
| vwap | -0.8 | +0.1 | +0.0 | +2.1 | +1.2 | +1.1 |
| ema | -1.0 | -0.5 | -0.5 | +1.8 | +1.3 | +1.1 |
| sweep | -1.4 | -1.0 | -1.5 | +1.7 | +2.5 | +3.6 |
| flag | +0.7 | +0.4 | +0.7 | +0.8 | +0.2 | -0.6 |
| ALL | -0.8 | -0.3 | -0.2 | +1.3 | +0.5 | +0.8 |

A strategy needs to clear 2bp of round-trip cost on top of the random row before any exit rule can help it. If these differences are ~0 at every horizon, the patterns carry no information and no stop, target, filter or position-sizing scheme recovers an edge that was never there — the only honest fixes are a different signal or a cheaper way to trade. If they are materially positive at some horizon while the bracketed backtest loses, then the bracket is the problem and the exit is worth real work.
