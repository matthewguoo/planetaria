# The mechanical arm's edge is the day, not the night — 2026-08-10

Question (Matthew): can overnight-held mechanical sleeves time-share capital
with the morning strategies by exiting at the open? The paper's t1_open dial,
evaluated on the delayed-entry mechanical arm (accept+60m entry, tape sides,
net 13bp, OOS 2019-26, walk-forward gate unchanged):

| arm | exit T+1 15:55 | exit T+1 OPEN |
|---|---|---|
| ungated | +39.5bp (t 2.01) | +10.6bp (t 0.84) |
| gated P>=0.45 | +81.2bp (t 3.33) | +15.7bp (t 0.99) |

Per hour of capital held, gated: 3.46bp/h holding to 15:55 vs 0.95bp/h
cutting at the open — the close exit is more capital-efficient per hour
DESPITE holding longer.

Reading: the delayed entry already forfeits the first-hour pop (that is its
design); what it harvests is the T+1 RTH continuation. It is not an
"overnight hold" in the capital sense — its dollar is genuinely busy from
~17:30 to next 15:55 and cannot be freed for the 09:28 auction without
gutting the sleeve. This inverts the flagship decomposition (night0 +123bp,
t1_open Sharpe 2.32) because the flagship enters AT the react print and owns
the night; the delayed entry sells that hour for contamination-immunity and
buys the day.

Consequence for the capital schedule: gff (minutes) and the fly (options
margin) are near-free overlays; the real allocation contest is day2_pop vs
delayed-gated for T+1-RTH dollars (corr -0.07); pure mechanical overnight
(mech carry react->open) stays measured-dead at 0.34. The only overnight
that pays big remains the LLM-gated first-hour entry, which the forward
test owns.

_Provenance: ad-hoc over research_wickout paths (i_t1open), walk-forward
identical to research_delayed_account.py; evening session 2026-08-10._
