# Pre-registration — `day2_pop` (day-2 earnings pop, stack component)

Registered 2026-08-11, before any live decision by this class. Evidence:
`research/pead-llm-gate/notes/day2_mech_20260810_0355.md` (the effect),
`day2_sim_20260810_0416.md` (account shape), `day2_shift_20260810_2123.md`
(the 09:32 entry). Numbers below are FROZEN; live results never edit them.

## Admission decision, stated plainly

The fund's standalone bar (Sharpe >= 1) is **explicitly waived** for this
class by Matthew's call (2026-08-11): day2_pop is admitted as a STACK
COMPONENT, not a standalone sleeve. Its risk-hours (T+2 09:32-15:55) are
the hours `gap_fail_fade`'s dollars are otherwise idle — the 09:32 entry
(which also measured +3.3bp better than the open, t 0.97) exists so gff's
auction round trip completes first on the same capital. Cross-sleeve
evidence: correlations |rho| <= 0.08 against every running sleeve; on the
delayed sleeve's 20 worst days day2 averaged +134bp.

## Frozen configuration

- Universe: AMC earnings reporters (estimate journal; Finnhub calendar),
  prior-session dollar volume >= $100M, price >= $5.
- Signal: clean-anchor reaction — close(first session after report) /
  close(last session at/before report) - 1 **>= +5%**. UP SIDE ONLY (the
  down side measured +2.6bp gross, t 0.26 — there is nothing there).
- Trade: T+2 (the second session after the report): enter long 09:32 ET
  marketable limit, exit 15:55 ET hard time stop. No tp, no sl.
- Slots: 4, ranked by prior-session dollar volume, 25% of allocation
  each. Instance: $10k allocation, 20% circuit breaker, note-mode first.

## Expectation bands (metric: net bp/trade at ~6bp costs)

- net bp/trade: **[+10, +30]** (backtest +29.2, t 3.12 — the band's top
  IS the backtest; the discounted center is the expectation)
- account Sharpe: [0.5, 0.9] (backtest 0.80)
- trades/day: [0.3, 0.8] (backtest 0.55 at 4 slots)
- win rate: ~50-53% (the edge is skew + drift, not hit rate)

## Stopping rules

1. 40+ trades with net bp/trade below 0 -> pause, re-derive.
2. Measured round-trip cost > 15bp on these books -> pause (the 6bp
   assumption is load-bearing; books are $100M+ so this should not bind).
3. Calendar coverage measured < 40% of panel-rate candidates (scan
   journals reporters/day; panel expectation ~0.55 taken/day) -> the
   universe is not the studied universe; pause and fix the feed.
4. 2022-style year (-47.6 net) is INSIDE the studied distribution — a
   drawdown alone does not stop the class; the breaker bounds it.

## Ladder

1. note-mode >= 20 sessions inside the bands (journals + sim fills);
2. one-share live >= 10 sessions (fill quality vs the 09:32 quote);
3. full size at $10k.

## Stated assumptions and known gaps

- BMO reporters are OUT of scope (panel is AMC; the BMO twin is its own
  queued study). The scan journals them as absent by construction.
- Finnhub calendar coverage measured ~2/3 of reporters — missed events
  are missed trades, not adverse selection (missingness is coverage-
  driven, not outcome-driven); stopping rule 3 watches it.
- Costs assume RTH marketable limits on $100M+ books (~6bp). The 15:55
  exit (vs the study's close print) was the shift study's own convention.
- 2016-2026 year table includes one badly negative year (2022) and two
  flat ones (2017, 2025) — the sleeve earns its seat via stacking, and
  the waiver above is the honest record of that decision.
