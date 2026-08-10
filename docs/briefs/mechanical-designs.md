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
$500M+ names, +9.6 on earnings mornings. Net after borrow and ~5-8bp
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

## 4. NEEDS-STUDY shelf (do the study before any code)

- **1DTE overnight fly**: does the QQQ premium pay for holding the
  structure overnight into expiry day? Unmeasured; same fetch harness as
  0dte-vrp with expiry = T+1. One script, one note.
- **Single-name earnings 0DTE/weekly fly**: selling event vol on the
  flagship's own watchlist names. The implied-move data exists (Alpaca
  options bars 2024-02+); the tail is nastier and the spreads wider than
  index products. Study must price the wings at real quotes.
- **Month-end Etula overlay**: the battery showed first-3-days +8.3bp
  (t 2.29) on 33 years but +3.1 (t 0.5) since 2016 — decayed; only worth
  revisiting as a timing overlay on flows the fund already trades.

## 5. Rejected for cause (measured; do not rebuild)

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
mandate: day-2 mechanical drift (RTH-only — no SIP gate), the 1DTE
overnight fly, single-name earnings flies. Each $10k-allocated and
breaker-bounded, every one pre-registered before its first live
decision.
