# Lead-up ACCOUNT simulation — 2026-08-05

Engine rules: risk 0.5%/name at vol-scaled stop (x2.5, 5%..12%, TP=2xSL), caps 20%/name 100% gross, max 10 entries/day, costs 3.0bp/side.

```
   entry    final  ret_pct  max_dd_pct  trades  tp  sl  deadline  skipped_full  win_pct
T3_close 135098.0     35.1       -16.8     971  42 150       779           521     52.3
T2_close 119208.0     19.2       -14.2    1138  20 111      1007           354     51.0
 T1_open 109623.0      9.6       -18.2    1493  22 131      1340             0     49.6

null (random entries, same machinery): mean 15.4% p5 -4.2% p95 37.7%
SPY buy-hold: 29.8%
```
