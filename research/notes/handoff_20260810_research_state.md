# Handoff — fund & research state after the 2026-08-10 session

Written at the end of the long Sunday/Monday session that (a) overhauled
maintainability (11 commits: check.ps1 + hooks, research_common,
terminal-residue deletion, strategy_id joins, ET unification, env pinning,
docs), (b) pushed 143 commits to origin, and (c) backtested the mechanical
book at account level. Every number below has a note with provenance; this
file is the map.

## 1. URGENT before the next market session

- **fly-1 credit-band skip — RESOLVED by Matthew in 19bf16b** (class
  default 0.25 -> 0.10, pre-reg amendment documented). Residual question
  worth one look: the running instance enforced the class default while
  its DB params read 0.10 — if instance params don't override class
  defaults in the guard path, that plumbing bites the next amended param
  too. Verify at Tue 14:00 that the decision cites [10%, 90%].
- **gff-1 journaled NOTHING through Monday's open** (enabled, note-mode).
  Created-after-open vs silent-no-signal vs broken morning path —
  distinguish; a quiet-day journal line would remove the ambiguity class.
- nosip-1 forward test is live (`live: true`); needs ~200 non-neutral
  verdicts (2-3 earnings seasons) per its pre-registration.

## 2. The measured books (all account-level, flat days in series, alpha/beta = daily OLS vs SPY)

Sleeves at REGISTERED configs, net of stated costs — details in
fly_account / gff_account+gff_decade / mech_account / delayed_account /
day2_shift notes (all 2026-08-10):

| sleeve | window | Sharpe | note |
|---|---|---|---|
| gff LONG | 10.6y | 0.99 @10bp / 1.25 @6bp | +14.5bp/tr t 3.27; PASSED 6y backward OOS test (2016-21 fetched fresh); cost decides everything; dead at 20bp |
| gff BOTH (needs shorts) | 10.6y | 0.96 @10bp / 1.38 @6bp | t 4.52 |
| fly 1-set | 2.5y max (no data/instrument earlier) | 1.26 at INTRINSIC only | 15:30-exit ZERO; **exit study DONE 2026-08-11 (fly_exit_curve note): 15:50 exit keeps ~36% of intrinsic, 15:59 keeps 51%, the 15:59->settlement step (+1.45bp/d t 4.7) is unharvestable (desk force-closes expiring positions); at 15:50 the years read 2024 +3.50 / 2025 -0.92 / 2026 +0.16 — BELOW the pre-reg band; amendment decision is Matthew's** |
| day2_pop @09:32 | 10.6y | ~0.80 | entry shift from open is FREE-to-positive (+3.3bp, t 0.97) → stacks with gff on shared dollars |
| delayed-entry mech ungated | 10.6y | 0.64-0.72 | +36.9bp/tr t 2.12 decade-stable; NOT an overnight play — open-exit guts it (+81→+16 gated); dollar busy 17:30→next 15:55 |
| delayed gated P>=0.45 (ML) | 2019-26 OOS | 1.16 | +81.2bp/tr t 3.33; AUC 0.737 stable each year; quarantined from high-confidence book (fitted model, family risk) |
| gff gated tau=0.50 (ML, added 2026-08-11) | 2019-26 OOS | **1.57** @10bp flat / 1.27 @measured books | +21.1bp/tr t 4.37 (+16.9 t 3.51 at per-trade NBBO); CAGR +11.5% flat / +9.1% measured vs ungated 1.24/0.88 & +10.8/+7.4%; maxDD 6-7% (halved); placebo-clean, crowdedness-concentrated (robustness note); MC layer (gff_gate_mc note): 28-fold CV lift +5.2bp central, 26/28 positive — the WF +8.8 is an 89th-pct draw, PLAN ON ~+5; paired bootstrap P(dSharpe>0)=0.93, gated Sharpe CI [0.89, 2.25]; QUARANTINED (fitted) — journal-only in gff-1 first, tau-skip only as pre-reg amendment (gff_gate_/gff_gate_robustness_/gff_exit_cost_ notes, designs brief §3e) |
| mech carry | 10.6y | 0.34 | dead, stays out |
| short-gap-up (minute universe) | 4.6y | — | REJECTED (+3.3bp net best, t~0.6) — anti-queue |
| CSP 0DTE (IRA-legal premium) | 2.5y | 0.52 on secured cash | REJECTED — +2.7%/yr on the strike cash it locks; the wings were the capital structure |

Books (equal capital, gff@10bp): all-mechanical (no LLM/ML)
gff_both+day2+delayed_ung+fly = **+11.0%/yr, Sharpe 1.62, maxDD 11.8%,
beta +0.01** over 2019-26; long-only version 1.49; no-AH version
(gff_both+day2+fly) 1.35 at maxDD 8.5%. 2024+ window shows 2.7-3.0 —
the flattered face, don't plan on it. Cross-sleeve correlations ~0.00.
With the ML-gated delayed swapped in: 1.69-1.83. Beta-overlay frontier
and 2x-leverage sweep are in the chat record + mech_book note: 2x book
≈ 20-22% at 17-24% maxDD post-haircut; margined SPY ≈ a wash vs ~8-10%
interest; margin interest on the alpha book itself ≈ nil (overnight
sleeves fit inside cash; intraday borrowing is never billed).

## 3. Wrappers

- **Taxable margin (the "anything book")**: full book + shorts + 2x
  post-gates ≈ 20-22% at Sharpe ~1.6. Needs: shorts verification
  (verify_short_paths.py), delayed-entry strategy built, cost
  measurements, fly bug fixed.
- **Alpaca Roth IRA** — supported now (Trading API; active account
  prerequisite). Constraints verified from docs: options to LEVEL 2 ONLY
  (no spreads → no fly), 1x limited margin (no leverage ever), no shorts.
  UNVERIFIED: extended-hours orders in IRA (make-or-break for the delayed
  sleeve) and PDT treatment — needs an IRA preflight script + a support
  ticket. Roth book if AH works: gff_long+day2+delayed_ung ≈ 10.2% at
  1.29 as /3; STACKED config (gff+day2@09:32 share dollars, delayed on
  its own slice) ≈ ~13-14% at lower Sharpe (~1.1) — stacking trades
  Sharpe for CAGR. Without AH: ~8.3% at 0.99.
- Roth tax fit is maximal (100% short-term gains) but Matthew's
  25%-CAGR-in-Roth goal exceeds the measured shelf at 1x by ~2x —
  see queue; goal-pressure guardrail applies.

## 4. Research queue (Matthew's direction: densify edge, not just add sleeves)

1. **ML gates on existing panels** (the +39.5→+81.2bp precedent): a
   fade-quality gate for gff (premarket features → take/skip) and a
   continuation gate for day2 (event-day tape). Same discipline as the
   wick study: walk-forward, thresholds counted as a family, forward test
   before trust.
2. **Fly exit study**: where between 15:30 (zero) and intrinsic (all of
   it) does the 15:50 exit sit; can a later exit + assignment handling
   recover the last-30-min edge? Needs minute marks past 15:30 (small
   fetch) or live measurement.
3. **BMO premarket twin** — the one genuinely new event clock; blocked on
   a pre-08:00 data purchase; write the spend memo.
4. **Chan-Marsh evening 8-K follow-on** — daily-bars replication first.
5. **Alpha-scan part 3** — the three web-literature agents that died at a
   session cap never reported; rerun.
6. gff LULD/halt-adjacency study (112 no-09:31-print events in the decade
   panel = the halt tail); slot-capacity is signal-limited (measured:
   2→4 slots adds ~+1%/yr only).

## 5. Engineering queue

1. fly-1 param plumbing bug (§1) — first.
2. **Confirmed-trigger ExitEnforcer mode** (dwell-10min @ -8% / RTH-only
   arming) — measured-free disaster insurance for every PEAD-family
   sleeve; research-approved, unbuilt.
3. day2_pop strategy class + pre-registration (entry 09:32 per the shift
   study), then instance in note-mode.
4. Delayed-entry mech strategy (§3d in the designs brief) + pre-reg.
5. Shorts verification → enable_short_leg on gff-1 (taxable path).
6. IRA preflight script (AH orders? PDT? L2 semantics) + support ticket.
7. docs/pre-registration-flagship.md is still MISSING (phase-15 Stage 0
   debt) — write before pead ever goes live.
8. Pre-register the six-month go-live scorecard itself (what the forward
   test must show for the 2027 Roth rollover decision).

## 6. Standing guardrails (unchanged, load-bearing)

Never push without asking (a push happened this session — explicitly
requested). Paper-lock stays. Notes are the record; write_note() now
stamps provenance. No new sweeps without pre-registration; CAGR targets
are outputs, not inputs. The 2026 drift weakness shows in the mechanical
arms too — regime, not gate failure; two more quarters answer it.

_Provenance: end-of-session handoff, 2026-08-10 ~21:30 ET; the session's
research commits run from "The mechanical book, measured at the account"
through "day2 at 09:32 is better than at the open"._
