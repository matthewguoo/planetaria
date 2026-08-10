# Wick-outs, filtered stops, and the head-fake question — synthesis, 2026-08-10

The ask: on binary, violent earnings releases, (1) is there an exit smarter
than dumb TP/SL — narrow protection that does not get wicked out; (2) can a
take-profit at the event's implied move beat fixed percents; (3) can the SIP
feed's first minutes tell a PERMANENT repricing from an initial move that
reverts, accurately enough to act on — by filter, by ML, by RL.

Everything below is the paper's own panel re-resolved at minute resolution:
1,962 |reaction|>=5% AMC events 2016-2026 (top-5/night, $50M floor), entry
at the last print 15 minutes after each event's own EDGAR acceptance; 1,800
of them now carry full SIP minute paths (release evening through the T+1
close, `cache/wick_minutes.parquet`, ~15MB, gitignored). Costs 13bp round
trip as everywhere in the study; every table re-stated at the measured
23.2bp + 15bp stop-fill slippage as a stress. Train 2016-23 / test 2024-26
throughout. Script: `scripts/research_wickout.py` (stages anatomy / rules /
fetch / mrules / learn / entry). Detail tables live in the five `wick_*`
notes and three `wick_*.csv` grids beside this file.

## 1. Anatomy: winners wick deep, so narrow stops are structurally doomed

Max adverse excursion before the T+1 close, EVENTUAL WINNERS only: p50
2.0%, p75 5.0%, p90 8.5% (in units of the event's own expected move: p50
0.6x, p75 1.7x). A 1% stop touches 75% of eventual winners; a 5% stop
touches 27% of them. Touch depth IS informative — P(win | touched -1%) =
45%, -5% = 27%, -8% = 20% against a 55% base — but never informative
enough: the touched-at--5% cohort still averages -397bp held to T+1,
BETTER than the ~-500bp a level-fill stop locks in there. Cutting at the
touch is worse than eating it, at every depth, before Sharpe even enters.

## 2. Mechanics: filters fix the wick problem and it still barely matters

Minute-resolved battery, stops filled at the NEXT minute close after
trigger (realistic engine latency), on both side policies:

| mechanism (8% depth) | tape dbp vs hold | FULL dbp | wick-out rate |
|---|---|---|---|
| hard stop, level fill | -45.4 | -57.8 | 21-23% |
| hard stop, next-close fill | -18.0 | -30.5 | 20-22% |
| confirm: 15m close beyond | -1.0 | -9.2 | 8-9% |
| dwell: 10 consecutive min beyond | **+4.0** | **+0.2** | **6-7%** |
| RTH-only arming | +0.2 | -0.4 | 8-9% |
| trail 8% armed at +3% MFE | **+7.1** | -9.9 | 38-40% |

Three facts. FIRST, the filters work as mechanisms: requiring ten
consecutive minutes beyond the level (or simply not arming stops in the
AH/premarket session) cuts the wick-out rate from ~20% to 6-9% — the
"else over dumb TP/SL" exists and is exactly the confirmation/dwell/
session-gating family. SECOND, even fixed, the stop adds almost nothing:
the best filtered stop is worth +4-7bp on the mechanical book and is
breakeven on the LLM book, because deep adverse touches that never recover
were mostly true losers whose loss was already taken by the time any
confirmed trigger fires. Its real value is disaster truncation at ~zero
mean cost — free insurance, not alpha. THIRD, the latency quirk: NEXT-CLOSE
stop fills beat level fills by 20-27bp — release-night wicks recover within
the minute, so a resting stop-market (fills at/through the level in the
sweep) is the worst instrument, and a trigger-then-market with a minute of
latency is the better stop. Narrow stops of any mechanism (2-3%, or
0.25-0.5xEM) stay firmly negative; VWAP-referenced exits fire on 87-94% of
events (post-release price lives at VWAP — wrong reference); stop-then-
re-enter cannot pay its second round trip (-17 to -21bp).

## 3. The take-profit is the Sharpe lever, and "at the implied move" is the wrong anchor

On the FULL-policy book, tp 10% / no stop: test +131.8bp vs +115.7 naked
(t 4.51 vs 3.56), grid-level account Sharpe (6 slots) 1.10-1.20 vs 0.81
naked, +117.3 under the stressed costs; tp 8% equivalent. Mechanism: it
clips the give-back tail of big winners (half of tp exits beat holding to
the close) and never touches a loser. The 2026-08-07 sweep quarantined the
10% target as best-of-171 never-out-of-sample; this study independently
re-derives 8-10% on a 2016-23 train half (tp10 train +118.5) that carries
to the 2024-26 test half (+131.8) at minute resolution with realistic
fills — upgraded from "sweep artifact" to "survives an honest split", with
the forward test still the decider.

TP at k x expected move (EM = point-in-time median |reaction| of the
symbol's last 4 events, median 3.3%): 0.5x/0.75x/1x all NEGATIVE dbp (-95
to -46 on FULL) — by construction these are >=5% movers, so the implied
move was already consumed by the reaction at entry, and an EM-anchored
target from the entry print takes profit into the fattest part of the
drift tail. The instinct "target the priced move" is right at the
PRE-PRINT anchor and wrong at the post-print entry; from entry, wide fixed
percents dominate every EM multiple tested.

## 4. The head-fake IS readable from SIP — and exits still can't monetize it

Walk-forward GBM (fit through year T-1, predict T, 2019-26), target = does
the reaction direction survive to the T+1 close:

| decision minute | OOS AUC (T+1) | AUC (T+3) | exit-policy value |
|---|---|---|---|
| accept+30m | 0.675 | 0.656 | +0.1bp |
| accept+60m | 0.737 | 0.685 | +3.4bp |
| T+1 09:25 premarket | 0.757 | 0.723 | **-7 to -17bp** |

Classification is real, stable every single year 2019-2026 (0.61-0.82),
and rises with time. The strongest single tell at every decision point is
`retrace` — the fraction of the initial reaction handed back (alone:
0.70-0.78 inverted AUC); then current P&L, VWAP edge, fraction of minutes
above VWAP. But the exit value of the knowledge is ~zero and turns
negative as accuracy rises: at +30m the doomed cohort has ALREADY absorbed
its full loss (exit -347.0 vs hold -347.3 on the fired trades), and by
premarket, bailing on classifier-doomed names sells the low. The market
prices the head-fake exactly as fast as the tape reveals it. This is the
exit-side twin of the latency study's "the tape reprices in the release
minute".

RL, at the scale 2k events honestly supports (tabular one-backup fitted-Q,
15-min ticks, state = phase x pnl-vs-EM x giveback): learns "bail from
adverse states, bank winners early" on 2016-23 and then replays 2024-26 at
-3.3bp vs +93.4 held — the learned exits are a regime bet wearing a policy,
and the 2024-26 regime paid holding. Do not deploy learned exit policies
from this panel.

## 5. Where the first hour DOES pay: the entry, not the exit

Delaying entry from accept+15m to accept+75m costs nothing on average
(+39.5 vs +38.5bp — the first hour nets zero drift). Gating that delayed
entry on the same walk-forward classifier (tape sides, no LLM, 1,518 OOS
events 2019-26):

| arm | taken | bp/trade | t |
|---|---|---|---|
| enter at react, ungated | 100% | +38.5 | 1.70 |
| enter at +60m, ungated | 100% | +39.5 | 2.01 |
| enter at +60m if P>=0.45 | 61% | +81.2 | 3.33 |
| enter at +60m if P>=0.55 | 48% | **+92.4** | **3.34** |

The confirmation gate doubles per-trade edge, mechanically. (The same
approved events "entered at react" show +299-413bp — that is selection on
interval information, NOT capturable; +81-92 at the +60m price is the
tradeable number.) This is the study's actionable output: a
CONFIRMED-DELAYED-ENTRY mechanical PEAD — added to the designs brief as
§3d, needing its slot-level account sim and pre-registration before any
instance.

## Verdicts, in one place

- Narrow stops on earnings nights: structurally negative, filters or not.
  The wick problem is real (20% of dumb stop-outs are murdered winners)
  and fixable (dwell/RTH-only cut it to 6-9%) — but fixing it only gets a
  stop back to ~breakeven.
- If the fleet wants disaster insurance on PEAD books: dwell-10min at -8%,
  or equivalently RTH-only arming at 8% — costs ~nothing, truncates the
  tail. An ExitEnforcer "confirmed trigger" mode (N consecutive marks
  beyond level) is the engine translation; not built.
- Take profit: 10% fixed (or none). Not EM multiples from entry.
- Head-fake detection from SIP: works (AUC 0.74-0.76 walk-forward), pays
  at the ENTRY (confirmation gate, +42-53bp/trade over ungated), pays
  nothing at the exit.
- RL exits: measured, regime-fragile, rejected.

## Limitations

SIP minute bars are trade aggregates — no NBBO, so AH fills are bar closes
with a slippage stress rather than quoted spreads; the 15-min-reaction EM
proxy is realized history, not the options-implied straddle (the live
engine can substitute the real one); grid-stage levels quantize to 0.5%;
the panel is the liquid top-5-per-night — thinner names wick worse than
this. All sweep results are search results: the specific best cells
(dwell-10m/-8%, P>=0.55) carry best-of-family risk even with the split,
and nothing here has been forward-tested.
