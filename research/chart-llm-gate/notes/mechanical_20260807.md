# The retail canon, mechanised — 2026-08-07

20,550 setups, 12 symbols, 2025-08-01..2026-07-31, 5m bars, 2bp round-trip costs. Every session of every symbol — nothing sampled.

## By strategy (pooled across symbols)

| strategy | trades | win% | mean net bp | t | median R bp | target% | stop% | null pctile |
|---|---|---|---|---|---|---|---|---|
| orb | 3,464 | 45.4 | -2.92 | -1.45 | 125 | 7 | 27 | 27.5 |
| vwap | 5,542 | 39.9 | -1.41 | -1.48 | 46 | 20 | 50 | 68.5 |
| ema | 6,865 | 38.3 | -1.89 | -2.42 | 38 | 20 | 51 | 43.0 |
| sweep | 1,728 | 33.2 | -1.46 | -1.08 | 26 | 30 | 65 | 67.5 |
| flag | 2,951 | 38.3 | +0.64 | +0.41 | 52 | 24 | 56 | 98.5 |

`null pctile` is the share of 200 random-entry portfolios that came in BELOW the strategy's mean, pushed through an identical bracket. 50 means the pattern adds nothing over entering at random; under 50 means random entries did better.

## By strategy x symbol (mean net bp, trade count)

| symbol | orb | vwap | ema | sweep | flag |
|---|---|---|---|---|---|
| AAPL | +0.8 (264) | -5.9 (456) | -5.2 (578) | -2.3 (139) | -0.4 (195) |
| AMD | -7.5 (284) | +1.2 (413) | -3.9 (558) | +3.0 (144) | +1.8 (393) |
| AMZN | -0.8 (274) | -2.2 (476) | -1.6 (574) | -6.5 (145) | -0.9 (266) |
| AVGO | -6.4 (270) | -0.9 (459) | -1.4 (592) | -3.3 (153) | -1.7 (339) |
| GOOGL | -9.6 (271) | -0.7 (483) | +0.0 (586) | -1.7 (147) | -2.0 (243) |
| META | -11.2 (290) | -3.2 (499) | -5.6 (624) | -1.8 (145) | +4.7 (248) |
| MSFT | -2.4 (275) | -5.1 (450) | -1.7 (555) | -2.4 (135) | +0.5 (194) |
| NFLX | -1.9 (268) | -0.6 (487) | -2.5 (579) | +1.0 (131) | -4.5 (290) |
| NVDA | -7.0 (289) | +2.3 (470) | -1.3 (593) | +2.5 (149) | -4.5 (255) |
| QQQ | -0.1 (336) | +0.7 (453) | -3.2 (561) | -4.2 (144) | -2.8 (108) |
| SPY | -2.1 (349) | -1.5 (463) | -1.9 (514) | -4.6 (141) | +2.6 (55) |
| TSLA | +12.3 (294) | -0.6 (433) | +6.1 (551) | +2.5 (155) | +10.7 (365) |

## Cost sensitivity (mean net bp per trade)

| strategy | 0bp | 2bp | 5bp | 10bp |
|---|---|---|---|---|
| orb | -0.92 | -2.92 | -5.92 | -10.92 |
| vwap | +0.59 | -1.41 | -4.41 | -9.41 |
| ema | +0.11 | -1.89 | -4.89 | -9.89 |
| sweep | +0.54 | -1.46 | -4.46 | -9.46 |
| flag | +2.64 | +0.64 | -2.36 | -7.36 |

The 2bp column is the headline. A strategy that only works at 0bp is a strategy that pays the spread to the market maker.
