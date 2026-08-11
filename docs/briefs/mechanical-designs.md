# Mechanical strategy designs — the fund's pipeline

Written 2026-08-10. Matthew's directive: the fund wants strategies that are
mechanical or majority-mechanical (the flagship is the one LLM bet under
forward test; everything else should not share its epistemic risk). Each
design below states its evidence, its blocker, and the exact next step.
Ordering is by readiness, not by expected Sharpe.

## 1. `fly-2` — the 15:30 tranche (READY when fly-1 graduates)

Same `afternoon_fly` kind, `entry_et: "15:30"`. Measured on the same 652
QQQ sessions: +1.9bp of S/day at t 2.87 for ~20 minutes of exposure — the
highest t-per-minute cell in the grid. Running it beside the 14:00 tranche
time-diversifies the short-vol book (the 14:00 entry owns more of the
afternoon's tail). No new code; a second instance with one param changed,
its own $10k allocation and breaker, its own pre-registration addendum.
Gate: fly-1 completes its 20-session note-mode stretch inside the band
first — two tranches of an unproven sleeve is sizing, not diversification.

## 2. `mech_carry` — the no-LLM earnings overnight carry (STUDIED 2026-08-10: PARKED)

> **VERDICT (`pead-llm-gate/notes/mech_carry_clean_20260810_0328.md`):
> the clean-anchor full-panel study vetoes running it.** Pooled it clears
> every bar (UP>=2%, top-5/night: +35bp gross, t 3.57, +12bp net at
> 23.2bp; +47/+24 at the 3% gate) — but the year table is a regime
> confession: 2020/2021/2024 earned +53..+75bp net and 2022-23 and
> 2025-26 are NEGATIVE net (2026: −64bp). The earlier +68bp/t 3.4 was
> the late-acceptance subset flattered by regime mix. The class is built,
> tested, and registered (`app/strategies/mech_carry.py`) so a future
> regime-filter study has something to gate — but no instance runs until
> a PRE-REGISTERED regime condition survives its own out-of-sample test.
> The gated-off short side is +2bp (t 0.2): there is nothing there either.

The original design note, kept for the record:

The decomposition measured the UNGATED signed carry (enter the after-hours
reaction print, exit next open): on acceptances >= 16:30, AH-UP events at
the 2% gate earned +68bp/night at t 3.4 long-only — no model, no verdict,
just "buy what just gapped up on real results, sell the open" (the same
clientele mechanism as everything else tonight: attention opens rich, and
the opener is the exit). At the measured 23.2bp round trip that is ~+45bp
net on ~40 events/season.

Blockers, in order: (a) the measurement lives on the anchor-clean subset
(acceptances >= 16:30, ~19% of events) — the study must be re-run on the
full panel with a clean pre-release anchor (the panel close, not the 16:05
print) before this is believed; (b) SIP, same as the flagship — the entry
is an after-hours fill. Build shape: `earnings_reaction` minus the LLM —
watchlist, re-anchor, gate, long-side-only carry, MOO exit. Majority
mechanical: 100%.

## 3. `gap_fade_short` — fade the morning gap-ups (BLOCKED: shorts)

From the bleed map: short-only, gaps +1% to +4% (skip the squeeze tail),
px >= $25, top-N by dollar volume, enter at/after the open, exit MOC.
Measured +6.4/+8.6bp/day gross in the band (t ~2.3), works in $100+/
$500M+ names, +9.6 on earnings mornings; the minute-level pass
(open-window note) refines the clock: enter 09:31, SHORT side only,
hold to noon or the close (+19.5 to +23.7bp/event, t ~3) — the 15-30min
scalp version is NOT supported. Net after borrow and ~5-8bp
execution: thin (+0-4bp/day) — this earns its seat as a DIVERSIFIER (short
book, intraday horizon, negative correlation to the overnight sleeves),
not as a headline. Gate: `equity_long_only` off + per-symbol ETB checks +
measured auction execution under ~8bp all-in. Pass two of the bleed study
(minute-bar hold curve) sets the exit clock first.

## 3b. `factor_combo` — SHELVED BY MANDATE 2026-08-10

> Matthew's call, same day the study landed: a 1.06 Sharpe at
> monthly horizon does not clear the fund's bar, and the mandate
> is MECHANICAL, INTRADAY TO A FEW DAYS. The evidence below
> stands unchanged for whenever a slow sleeve is wanted; nothing
> further is spent on it now.

Multi-factor long-only across the monthly top-500 (value + book +
quality + momentum percentile ranks, top 50 equal-weight, monthly,
point-in-time EDGAR fundamentals): **+21.4%/yr net, Sharpe 1.06, maxDD
22.5%, stable across halves (1.07/1.05)** vs SPY +15.0%/0.96 and vs its
own equal-weight-universe control +13.1%/0.73 — the edge is selection
(+8.3%/yr over EW), not the EW tilt. The breadth product one person
cannot run by hand, and the ex-div execution overlay's natural customer
(research/exdiv/: +3-4bp per ex-date crossing, free at rebalance).
Before build: sector-tilt x-ray, drawdown-path read on daily data, and
a pre-registration. Single factors do NOT earn seats alone (all <= 0.85
Sharpe vs SPY 0.96).

## 3c. `day2_pop` — THE MANDATE'S TOP CANDIDATE (studied 2026-08-10)

Long-only, RTH-only, no SIP, no LLM, no shorts: after an UP earnings
reaction (clean anchors), buy the SECOND session's open, sell its close.
>=5% gate: +31.8bp gross, t 3.23, +25.8bp net@6; >=10%: +57.0/t 2.79/+51
net. Down side ~zero (nothing gated away). 9 of 11 years positive (2022
the exception). ~150/yr events at the 5% gate. Before instance: the
slot-level account sim, then pre-registration. This is the first
candidate the CURRENT account could run live with no billing change.

## 3d. `pead_confirmed` — confirmed delayed entry (studied 2026-08-10)

The wick-out study's actionable output (wickout_synthesis_20260810.md):
delay the AMC entry from accept+15m to accept+75m (costs nothing — the
first hour nets zero drift) and take only events whose first hour of SIP
tape confirms the reaction (walk-forward GBM on retrace/VWAP/consistency
features, or the poor-man's version: still holding >=X% of the move and
above post-release VWAP). Tape sides, no LLM: gated +81-92bp/trade
(t 3.3) on 48-61% taken vs +39 ungated, OOS 2019-26. Same study's exit
verdicts apply fleet-wide: no narrow stops on event books ever (20% of
dumb stop-outs are murdered winners); disaster insurance if wanted =
dwell-10min-beyond--8% or RTH-only-8% (~free); take profit 10% fixed,
never EM-multiples-from-entry. Before instance: year-by-year stability
table, slot-level account sim, pre-registration; decide LLM stacking
(the gate reads the tape, the flagship reads the filing — independent
channels) only after the mechanical version has its own numbers.

## 3e. gff fade-quality gate — ML take/skip on `gap_fail_fade` (studied 2026-08-10)

Queue #1 of the handoff, delivered: walk-forward 2019-26 (train from
2016), 23 decision-time features (nothing at/after 09:30), primary cell
pre-declared before results (HGB, both legs, tau 0.50, net@10).
Evidence: `research/open-window/notes/gff_gate_20260810_2339.md` —
**+12.3 -> +21.1bp/trade at 61% keep (t 4.37), beats its own
random-skipping null at p=0.003; account level Sharpe 1.24 -> 1.57 with
maxDD 13.4% -> 6.0% and slightly HIGHER annual return on 40% fewer
trades.** Rescues the bad years (2019: -5 -> +20; 2023: -3 -> +14),
gives back -9bp in 2026. Top features: fade-crowdedness that morning,
prior-day return, turn magnitude, premarket dollars. Honest negatives:
rank-by-P slot selection LOSES to the registered pmvol ranking (the
registered selection stands), and the 09:00->09:15 premarket segment
adds nothing. Family risk is the delayed-gate precedent's: a fitted
model stays quarantined from the high-confidence book until a forward
test. Deployment path: journal-only first — gff-1 computes and journals
P per candidate each morning (every feature is in the scanner's hands by
09:27) for a season, then a pre-registered tau=0.50 skip rule lands as a
pre-reg amendment. The gate only decides which candidates submit; MOO
mechanics never change.

## 4. NEEDS-STUDY shelf (do the study before any code)

- **Single-name earnings 0DTE/weekly fly**: selling event vol on the
  flagship's own watchlist names. The implied-move data exists (Alpaca
  options bars 2024-02+); the tail is nastier and the spreads wider than
  index products. Study must price the wings at real quotes.
- **Minute-tape ML at scale (the 3090 project)**: the one workload where
  the RTX 3090 (24GB, verified 2026-08-10; torch NOT installed) genuinely
  changes what is testable. Scope: build a top-100-liquid full-session
  minute cache 2022-2026 (~40M bars, ~2-3GB, one overnight resumable
  fetch); train cross-sectional k-minute-ahead models with the house
  protocol (walk-forward by year, train-label placebo, thresholds as
  families — the gate study's exact discipline). ORDER OF BATTLE: a CPU
  GBM on engineered bar features is the baseline any deep model must
  beat; only then TCN/small-transformer sequence models on GPU. Setup
  rule: torch goes in a SEPARATE research venv (`research/.venv-ml`,
  `pip install torch --index-url https://download.pytorch.org/whl/cu121`)
  — NEVER into backend/.venv while the engine runs from it. Honest
  prior stated up front: minute-horizon alpha must clear ~6-10bp retail
  costs, and the repo's own evidence (GHLZ dead at our latency, learned
  exits rejected) says most of what a net finds will be cost-dominated;
  the deliverable is a measured yes/no, not a promised sleeve. Event
  panels (n~10^3) stay OFF the GPU — deep nets at that sample size are
  overfit machines and the tabular gate already owns that ground.
- **Month-end Etula overlay**: the battery showed first-3-days +8.3bp
  (t 2.29) on 33 years but +3.1 (t 0.5) since 2016 — decayed; only worth
  revisiting as a timing overlay on flows the fund already trades.
- **Options flow (the retail "unusual activity" canon)**: testable, but
  it needs its own fetch design — Alpaca has no historical chain
  snapshots, so chains must be RECONSTRUCTED: strikes near spot x 0-2
  nearest expiries -> OCC symbols -> OPRA minute bars with volume.
  Scoped v1: SPY+QQQ+10 liquid single names, 2024-02+, ~7.4k batched
  requests (~1.5h overnight fetch). Features: call/put volume and
  notional by moneyness bucket, day-over-day z-scores, OTM-call spike
  flags; label next-day underlying return; GBM walk-forward + battery
  cells (P/C extremes, sweep proxies), the 2b bar throughout. Prior
  stated: most published "flow" signals are marketing; the measured
  yes/no is the deliverable. (2026-08-11, Matthew's canon list.)

## 5. Rejected for cause (measured; do not rebuild)

1DTE overnight fly (+1.6bp/d, t 1.2, Sharpe 0.76, 2026 negative — the
gap eats the credit; flies_1dte note); opening-window scalps (the gap
fade does NOT complete by 10:30 — it bleeds to noon/close, and the
scalp cell is year-unstable; open-window note); buying AM dumpers (-15
to -21bp, t -2 — morning weakness continues); shorting AM rippers (~0);
SPY afternoon fly (implied 0.51% vs delivered 0.48% — no premium to
sell); overnight-only retail baskets (dominated by holding); GHLZ
last-half-hour momentum (gross-negative post-publication); candle-pattern
anything (dead to inverted); daily-churn reversal (the market maker's
paycheck, not ours).

## The fund's shape under the 2026-08-10 mandate

Mechanical, intraday to a few days, nothing slower. Running: fly-1
(2h hold). In class: fly-2 (20min). Parked behind named gates, all
in-horizon: gap_fade_short (intraday, shorts), mech_carry (overnight,
regime), the flagship (T+1/T+3, the one LLM bet). Open studies in
mandate: day-2 mechanical drift (RTH-only — no SIP gate), confirmed
delayed entry (§3d), the 1DTE overnight fly, single-name earnings flies.
Each $10k-allocated and breaker-bounded, every one pre-registered before
its first live decision.
