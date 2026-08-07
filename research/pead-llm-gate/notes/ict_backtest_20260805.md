# iFVG / PO3 mechanical backtest — 2026-08-05

Symbols SPY,QQQ,AMD,TSLA, 2025-02-01..2026-08-01, SIP 1m base.
Definitions + methodology: `backend/scripts/research_ict_backtest.py` docstring.

```
strategy symbol  tf  trades  win_pct   avg_R   pf  mean_$      t  null_pct
    iFVG    SPY   1   26882     29.3  -2.566 0.67 -0.0386 -21.36      92.0
     PO3    SPY   1     228     30.7  -0.268 0.62 -0.1772  -2.16       0.0
    iFVG    SPY   5    4936     29.2  -2.062 0.75 -0.0534  -6.12      71.0
     PO3    SPY   5     201     28.9  -0.226 0.80 -0.1221  -1.10      21.0
    iFVG    SPY  15    1526     30.5  15.167 0.82 -0.0588  -2.52      65.0
     PO3    SPY  15     170     32.9  -0.103 0.81 -0.1637  -1.07      11.5
    iFVG    QQQ   1   27345     29.1  -2.366 0.69 -0.0417 -19.91      87.5
     PO3    QQQ   1     218     36.7  -0.070 0.57 -0.2852  -2.53       0.0
    iFVG    QQQ   5    4854     29.6  -1.432 0.83 -0.0416  -4.08      97.0
     PO3    QQQ   5     197     28.4  -0.260 0.67 -0.3023  -1.97       0.5
    iFVG    QQQ  15    1465     30.4  -0.421 0.79 -0.0820  -2.97      39.0
     PO3    QQQ  15     167     27.5  -0.282 0.59 -0.5471  -2.50       0.0
    iFVG    AMD   1   26101     22.4 -12.076 0.48 -0.0994 -34.48     100.0
     PO3    AMD   1     229     31.9  -0.415 0.68 -0.3419  -1.21       0.0
    iFVG    AMD   5    4732     27.9  -5.079 0.68 -0.1035  -7.27      99.0
     PO3    AMD   5     208     29.3  -0.355 0.86 -0.1517  -0.53      34.0
    iFVG    AMD  15    1427     30.9  -1.625 0.76 -0.1135  -2.92      96.0
     PO3    AMD  15     176     31.2  -0.249 0.69 -0.5022  -1.25       1.0
    iFVG   TSLA   1   24389     27.2  -8.944 0.58 -0.0986 -29.45      92.0
     PO3   TSLA   1     229     34.5  -0.169 0.89 -0.1000  -0.63      55.0
    iFVG   TSLA   5    4662     30.1  -4.874 0.76 -0.0996  -6.41      95.5
     PO3   TSLA   5     211     34.1  -0.150 0.70 -0.4455  -1.93       2.5
    iFVG   TSLA  15    1489     32.6  -0.311 0.94 -0.0371  -0.80     100.0
     PO3   TSLA  15     169     34.3  -0.114 0.92 -0.1328  -0.38      38.5
```

## Reading

24 configurations, ~130k trades, 18 months (2025-02..2026-08), 4 symbols,
costs = 2 spreads/round trip. **Zero configurations were net positive.**

1. **iFVG ≈ random minus costs.** Its null percentiles are mostly HIGH
   (65-100): random entries pushed through the same 2R bracket lose about
   the same. The pattern adds no information; the losses are the bracket's
   win-rate asymmetry plus cost bleed. On 1m the zones (~cents) are
   smaller than the spread — structurally untradeable regardless of edge
   (avg_R there is dominated by costs; the 15.2 avg_R outlier is a
   tiny-denominator artifact, trust mean_$/t).
2. **PO3 (sweep-reclaim fade) was ACTIVELY anti-predictive.** Null
   percentiles 0.0-2.5 in seven configs: random entries beat it. Fading an
   overnight-range sweep in this period meant fighting momentum -
   the sweep tended to CONTINUE, not reverse. If anything the data hints
   the tradeable version is the opposite trade (breakout-continuation),
   which is... not the ICT story.
3. Caveats stated plainly: this tests ONE mechanical reading of each
   concept, one regime, fixed 2R brackets, no killzone/HTF-bias filters.
   A discretionary practitioner will always be able to say "that's not
   how I trade it." But that is exactly the astrology property: if the
   raw patterns carried signal, SOME slice of 24 configs should have
   cleared random. None did.

Verdict: **astrology, as tested.** The strategy-runner plumbing could host
either in an afternoon (the plugin contract is the easy part) — but the
data says there is nothing here to host. If you want a session-structure
strategy, the breakout-continuation inversion of PO3 is the only thread
worth pulling.
