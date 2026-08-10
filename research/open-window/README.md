# open-window

**Feeds the strategy: `gap_fail_fade` (instance gff-1).** Pre-registration:
`docs/pre-registration-gap-fail-fade.md`.

The first ninety minutes, at minute resolution, on the liquid morning-gap
universe (top-8 by |gap| x dollar volume, |gap| >= 1.5%, 2022-2026): where
does the fade complete (answer: it doesn't — it bleeds all day), does
momentum ignition survive a stop grid (no — stops are whipsaw churn and
every long cell is negative), and what does the LATE PREMARKET read buy
at the auction print (the strategy: when the 09:15->09:29 tape turns
>= 20bp against the gap, fading the gap AT the open earns +23.9bp gross
in one minute, t 5.85, both directions — and entering at 09:31 instead
erases all of it).

```
scripts/research_open_window.py   fetch|score      the fade/checkpoint curves
scripts/research_ignition.py      fetch|score|fetch_pm|score_pm
notes/                            dated results; failed_gap_split has the build's numbers
cache/                            SIP minute marks and paths (gitignored, regenerable)
```
