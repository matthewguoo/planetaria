# IRA preflight — what the docs say, what only a ticket/probe can answer

Researched 2026-08-11 ~03:00 ET (web). Feeds the Roth rollover decision
(~$50k, planned after the 6-month forward test).

## Confirmed from Alpaca's own pages

- IRAs run **limited margin (1x)**; shorting disabled; **options to
  level 2 only** (no spreads -> no fly, ever). Sources:
  docs.alpaca.markets/us/docs/ira-accounts-overview,
  alpaca.markets/support/can-ira-trade-on-margin-short,
  alpaca.markets/support/can-ira-trade-options.
- The general Trading API supports extended-hours submit AND fill
  (alpaca.markets/support/extended-hours-trading) — but nothing
  IRA-specific is documented either way.

## The two load-bearing unknowns (ticket + probe on a real IRA)

1. **Does "limited margin" mean the industry-standard thing** (trade on
   unsettled proceeds, no good-faith violations — the entire purpose of
   limited-margin IRAs at Fidelity/Schwab et al.)? If YES: GFV is a
   non-issue and the binding rule becomes **PDT — $25k minimum equity to
   day-trade** (the $50k rollover clears it with buffer; gff and day2
   are day-trade sleeves). If NO (cash-account semantics): every
   same-day round trip bought with T+1-unsettled proceeds is a GFV —
   three in 12 months = 90-day restriction — and the day-trade sleeves
   need either two-bucket capital rotation (halves per-dollar CAGR) or
   they stay in the taxable account.
2. **Do `extended_hours=True` orders work in an IRA?** Decides the
   delayed sleeve (the ~+2-4%/yr difference between the 8.3%@0.99 book
   and the 10.2-14% configs).

## Worst-case Roth shape (if limited margin is cash-like)

Roth gets ONLY the overnight/swing sleeves (no same-day round trips, so
no GFV exposure: buy close with settled cash, sell next open, repeat
next close on settled funds): delayed-entry (if AH works) + a
close_fade-style re-cut if one ever passes pre-registration. The
day-trade sleeves (gff, day2) stay taxable-margin. Best case (standard
limited margin + AH): the full Tier-1/2 long-only book at ~10-14%.

## Not the answer: "do everything in options"

L2 = long options, covered calls, CSPs. Long options structurally PAY
the variance premium this book harvests (the fly SELLS it); the
IRA-legal premium harvest was measured and rejected (CSP-side 0DTE:
0.52 Sharpe on secured cash, note csp_side_20260810). And 0DTE option
spreads at the open dwarf the 7.5bp median stock books gff crosses.
Options do not transplant equity alphas; they replace them with worse
ones.

## Support ticket draft

> We are evaluating opening a Roth IRA (Trading API). Three questions:
> (1) Does the IRA's limited margin permit trading with unsettled
> proceeds (i.e., are good-faith violations inapplicable, as with
> limited-margin IRAs at other brokers)? (2) Do FINRA pattern-day-trader
> rules apply to the IRA (min $25k equity to day-trade)? (3) Are
> extended-hours orders (extended_hours=true limit DAY) supported in IRA
> accounts, both pre-market and after-hours? If any of these are
> account-configuration dependent, what are the prerequisites?

## Preflight probe script (once an IRA exists)

Mirror verify_equity_paths.py on the IRA keys: (a) extended_hours limit
order submit at 17:30 ET — accepted? filled?; (b) same-day round trip
sized $100 — does the account report a GFV/cash restriction?; (c) TIF
cls/opg acceptance; (d) options chain quote + single-leg L2 order.
