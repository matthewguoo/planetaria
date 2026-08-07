# Overnight alpha (16:00 → 09:30) — 2026-08-05

Companion to latency_band_research_20260805.md. Question (Matthew): research
alpha overnight. Sources: two deep web-research passes (overnight-alpha
evidence; Blue Ocean venue mechanics — both peer-review-tiered with URLs, in
session transcript) + one in-house probe across the late-July 2026 earnings
season. Tags: [PR] peer-reviewed, [WP] working paper, [EX] venue/official,
[V] folklore, [OURS] measured here.

## 1. [OURS] Season probe: the AH-digested move carries into the open

All AMC reporters 2026-07-20..08-01 (Finnhub calendar), top-liquidity names,
close>=$5, $vol>=$50M, |AH move by 19:59| >= 2%, real AH prints: n=83.
Legs signed by AH-move direction; gap = last AH price (<=19:59 ET) -> next
09:30 open; morn = open -> 10:00. Script: session scratchpad
(overnight_season_probe.py); gross of costs.

```
                       gap (19:59->open)        open->10:00
all (n=83)            median +0.74%  60% cont.   +0.43%
AH up   (44)          median +0.99%  70% cont.
AH down (39)          median -0.28%  49% (coin flip / slight bounce)
|AH| >= 5% (46)       median +1.13%  61% cont.
```

Tails are violent: ARM's 19:59 AH price was 21.7% BELOW its next open (the
short-side catastrophe `equity_short_overnight=false` exists for); AXTI
gapped +14.7% then -16.3% by 10:00; COHU -13.5% morning after +5.1% gap.
Median good, distribution brutal.

**Reconciliation with the "PEAD is dead" literature** (Warp Speed JFE 2025;
Martineau CFR 2022 [PR] — no continuation minutes after the jump, large-cap
PEAD dead since ~2006): our anchor is different. This is not the T+minutes
chase (which our 8/4 probe already showed loses); it is the FULL overnight
hold of a 4-hours-digested move, exiting INTO the open. Three documented
mechanisms plausibly compose the carry: (i) SUE/earnings-momentum profits
accrue ~100% overnight (Lou-Polk-Skouras JFE 2019 [PR]); (ii) attention
names open RICH on retail open-buying (Berkman JFQA 2012 [PR]) — the exit
sells into that documented richness (and the AXTI/COHU morning fades are its
other side); (iii) part of measured overnight returns sits in the open print
itself (Bogousslavsky JFE 2021 [PR]) — attainable for us via `opg` (MOO)
exit orders. One season, one hot-AI regime — needs 2+ more seasons replayed
before note-mode.

## 2. What the literature kills and keeps overnight

Dead [PR-tier evidence]: nightly market overnight-premium harvest (2.6bp/
night vs 25-90bp overnight spreads; NightShares ETFs died of it; the 2-3am
drift itself ~0 since 2021 — NY Fed "Disappearing Overnight Drift" 2026);
pre-FOMC drift (gone post-2015); large-cap AMC chasing at 20:05 (exit
liquidity); unconditional gap fade/continue rules (folklore); ADR parity
means (4.9bp vs frictions); overnight passive making (realized spreads are
negative EVEN for Virtu/Jane Street on the liquid set); anything needing an
overnight short (venue+locate support absent-to-unverified).

Kept, ranked for our stack:

**2.1 BMO premarket reaction — the flagship literature fit.** Pre-open
announcers get 36% weaker initial reactions than AMC and drift ~4 days; the
stated cause is human processing time (Lyle et al.; Kellogg summary) — an
LLM-in-the-loop stack is the exact counter, and premarket books in liquid
names are the tightest of all off-hours windows, with informed trading
concentrated preopen (Barclay-Hendershott RFS 2003 [PR]). BMO cluster is
06:00-08:30 ET. Our gap: real-time premarket data before 08:00 (IEX silent
04:00-08:00; SIP delayed on this tier) — ATP closes it. Build = the
symmetric morning twin of earnings_reaction, same feeds, same journal.

**2.2 The probed §1 trade — late-AH entry, open exit.** Verdict-gated (LLM
direction agrees with tape), LONG side only (the side that works and the
side we have), liquid names, enter 19:45-19:55 on exchange AH books (NBBO
still exists until 20:00 — never pay Blue Ocean taker spreads), hold the
night doing nothing, exit MOO or into the first minutes. Unfilled DAY limits
auto-roll into the 04:00 premarket (~40x liquidity step) as the fallback
ramp [EX].

**2.3 8-K overnight follow-on drift (Chan-Marsh 2024 [WP]).** Extreme-miss
names show pronounced OVERNIGHT post-announcement drift driven by
unscheduled 8-Ks landing into thin books — our EDGAR poller is pointed at
exactly this; magnitudes unpublished, so replicate 2022-2026 on our data
first. Natural short-side candidate post-flip; long-avoid meanwhile.

**2.4 Asia-hours event taking on the overnight leaders.** The 20:00-04:00
session is ~80% APAC flow, retail-herded (|OI| 0.20-0.26), quoted by
essentially two MMs (Virtu, Jane Street; Citadel abstains) — and on liquid
names TAKERS beat the MMs on average (5-min price impact 32bp > effective
spread 28bp; realized -4 to -7bp) [Eaton-Shkilko-Werner WP 2025]. Two-thirds
of overnight moves never reverse — the game is being early WITHIN the night
on real events (DeepSeek night: $2.7B venue record hours before US desks
woke), not fading it. Catalysts are foreign: Nikkei/Kospi opens 19:00/20:00
ET, China prints 21:00-22:00, BOJ untimed, TSM monthlies ~01:30-02:00,
Europe 03:00. Universe: the ~50 symbols that actually quote (top 10 = 42%
of notional: TSLA NVDA QQQ TQQQ BABA SPY SOXL MSTR SLV SQQQ; ETPs = 61% of
volume). Needs: BOATS real-time (ATP), an Asia event calendar in the news
plane, and ideally an index reference feed (we have no futures view — open
data gap; BO QQQ/SPY books are the poor-man's reference). MARGINAL until
those land; the only true 20:00-04:00 strategy worth building.

**2.5 04:00 collar-release watcher [OURS, hypothesis].** Blue Ocean runs a
±20% static collar vs the prior 7:30pm last sale — a name whose news is
worth more CANNOT reprice all night (orders rejected); price discovery is
structurally postponed to the 04:00 exchange open. Scheduled dislocation,
zero literature: measure, then decide.

**2.6 Free overlays.** LPS execution timing (hold momentum/SUE-aligned
exposure overnight, enter late day, exit at open; never carry the
anti-aligned leg); Berkman prohibition (never BUY the open of a name the
news plane flagged overnight — sell into it); day-of-week (avoid speculative
longs across the weekend boundary — Birru JFE 2018); month-end T-1..T+3
long bias (Etula RFS 2020); FOMC/CPI as risk calendar, not drift.

## 3. Venue reality (operating constraints, all [EX]/[MEASURED])

- No NBBO, no LULD, no auctions overnight; ±20% static collar; $5M/100k-sh
  order caps; limit-only DAY/GTC; busts only beyond ±26-40% of a STALE
  reference (your bad fill stands); venue can mass-cancel in a capacity
  event (did: Aug 5 2024, 81 minutes of fills) — size every overnight
  position to survive no-exit-until-04:00.
- Spreads: ~28bp effective liquid set / 65-90bp beyond it (2/3 of the
  premium is market power, not adverse selection). Post, don't cross —
  except event-gated informed taking (§2.4), which is the documented
  exception.
- Liquidity humps 21:00 and 03:00 ET; trough 23:00-02:00; <2,000 symbols
  print at all most nights; books outside top ~50 one-sided for hours.
- Trade-date gotchas: overnight fills stamp NEXT day's trade date (ex-div
  disqualification buying on ex-eve); SIP is blind to 20:00-24:00 BO prints
  until ~08:15 TRF reporting (third-party "overnight return" datasets are
  incomplete — backtest on the BOATS feed itself); halt list emailed 2h
  pre-session; SSR carries overnight per SEC FAQ, Alpaca handling unprobed.
- No options overnight on Alpaca (index options are paper+RTH-only there);
  Cboe GTH (SPX/XSP, ~115k ADV) needs IBKR/tastytrade. Overnight
  instruments here = shares/ETPs only.
- Single-venue era ends ~late 2026-2027 (Arca 22h, Nasdaq 23h filed, EDGX
  24x5, 24X, NSCC 24/5 clearing) — spreads should compress; re-verify
  mechanics quarterly; SEC 24h-trading roundtable 2026-09-17.

## 4. Probe queue (overnight-specific)

1. Free: replay §1 across 2+ more earnings seasons (needs nothing new).
2. Free: BMO-reaction minute-path study on our calendar universe (the §2.1
   underreaction, measured on our names, premarket bars are historical SIP).
3. Free: Chan-Marsh replication — evening 8-Ks after big misses, overnight
   drift magnitude 2022-2026.
4. ATP-gated: log BOATS BBO continuously for the top-50 overnight set;
   spread-by-hour table; Asia-print -> BOATS reaction latency (TSM complex).
5. ATP-gated: collar-pin detector — nights where |AH move| approaches 20%
   vs the 7:30pm ref; measure 04:00-04:15 repricing behavior.
6. Paper probes: overnight short attempt (documented-absent — verify),
   SSR-flagged name behavior, pre-8pm order queueing, 03:50-04:05 rollover
   fill bursts on resting limits.

## 5. Core citations

Tug of war / overnight accrual: Lou-Polk-Skouras JFE 2019
(personal.lse.ac.uk/polk/research/TugOfWar.pdf) · CAPM at night:
Hendershott et al. JFE 2020 (ssrn.com/abstract=3117663) · Open-print
component: Bogousslavsky JFE 2021 · Overnight drift dead: NY Fed Liberty
Street 2026-07 (libertystreeteconomics.newyorkfed.org/2026/07/the-
disappearing-overnight-drift/) + ssrn.com/abstract=7035838 · Costs kill
churn: Lachance RFE 2023 (onlinelibrary.wiley.com/doi/full/10.1002/rfe.1180)
· Around the clock: Bondarenko-Muravyev JFQA 2023 (ssrn.com/abstract=
3596245) · AH efficiency: Jiang et al. JFQA 2012 · No jump continuation:
arxiv.org/abs/2601.08962 · PEAD dead: ssrn.com/abstract=3111607 · SUE
overnight: LPS Table (above) · 8-K overnight drift: Chan-Marsh
ssrn.com/abstract=4765828 · BMO underreaction: Lyle et al.
ssrn.com/abstract=3064160 · Preopen discovery: Barclay-Hendershott
ssrn.com/abstract=207914 · Nocturnal trading (the overnight-session paper):
Eaton-Shkilko-Werner ssrn.com/abstract=5181159 · Overnight adverse
selection: Lim ssrn.com/abstract=6610883 · NYSE Night Moves:
nyse.com/data-insights/night-moves-what-trades-and-when-in--the-overnight-
market · Attention opens rich: Berkman ssrn.com/abstract=1625495 · Day of
week: Birru ssrn.com/abstract=2715063 · Dash for cash: Etula RFS 2020 ·
Blue Ocean rules/collar/CE: blueocean-tech.io FIF deck 2026-03 + CE policy
2025-05 PDF · Aug 2024 busts: marketsmedia.com/blue-ocean-ats-resumes-
after-cancelling-trades/ · Alpaca 24/5: docs.alpaca.markets/us/docs/245-
trading · Full reports: session transcript 2026-08-05.
