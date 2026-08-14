# sr-levels

The "HOW TO SET STOP LOSS" meme, taken at its word: detect its three level
patterns mechanically on 5-minute candles — rising/falling **trendline**
(stop at the line), horizontal **breakout** (stop at the broken level),
**zone to zone** (stop past the range extreme, target the far zone) — and
ask (a) do the trades pay, and (b) can a 0DTE/1DTE option carry them. Same
universe, bracket engine, costs and random-entry null as `chart-llm-gate`'s
mechanical arm, so the two notes read as one table of eight retail setups.

```
scripts/research_sr_levels.py   fetch|build|report|wrap
notes/                          dated results, one per question
cache/                          bars + IV proxies (gitignored, regenerable)
```

Headline (levels note, 3,853 trades, 12 symbols, 60 sessions): all three
negative net of 2bp costs. `trendline` is the interesting one — null
percentile 2.5, random entries beat it: the meme's stop AT the line is a
12bp-median stop that gets whipsawed on 65% of trades, the same
anti-predictive signature as the archived PO3 sweep fade. `breakout` and
`zone` sit inside their nulls (76th/41st percentile): indistinguishable
from entering at random through the same bracket.

The 0/1DTE wrap (BSM-modeled on SPY/QQQ, flat IV, so at-least-this-bad):
wrapping a ~zero-edge underlying signal in a long ATM 0DTE costs −17% to
−45% of premium per trade (theta + a spread that is ~40–100x the share
spread as a fraction of capital at risk); 1DTE cuts that to −2% to −10%.
An option cannot manufacture edge the underlying signal does not have.

Data caveats, honestly: this container is keyless, so bars are Yahoo's
trailing 60 days (fetch stage) rather than the SIP year, and option P&L is
modeled rather than printed. `load_bars` prefers the `chart-llm-gate` 1m
SIP cache when it exists, so the dev box re-runs the full year with no code
change; real 0DTE prints go through the `0dte-vrp` fetch machinery. Neither
upgrade is expected to change the sign of anything above.
