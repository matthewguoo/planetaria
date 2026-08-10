# 0dte-vrp

**Feeds the strategy: `afternoon_fly` (instance fly-1).** Pre-registration:
`docs/pre-registration-afternoon-fly.md`.

Is the 0DTE variance premium still paid, and in what shape can this
account hold it? The ingredient first (ATM straddle at three entry times,
implied vs delivered), then the defined-risk sweep (iron flies, entry x
width), then the shield study (what removes the worst days — answer:
nothing that doesn't cost more than it saves; sizing is the shield), then
the 1DTE overnight variant (rejected: the gap eats the credit).

```
scripts/research_0dte_straddle.py   under|probe|fetch|score|wings|flies|shield|marks1d|flies1d
notes/                              dated results, one per question
cache/                              OPRA minute marks (gitignored, regenerable)
```

Headline (flies note): QQQ 14:00 entry, 1.0% wings — +3.2bp of S/day,
t 3.51, worst day -68bp bounded, gross Sharpe 2.20; the whole 1.0% row
clears t 2.8-3.5 at every entry. SPY measured thin (no build).
