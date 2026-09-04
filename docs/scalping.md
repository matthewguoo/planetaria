# Scalping with the terminal: fast bars, the spread optimizer, the SCALP profile

Three things landed together on 2026-09-04 so a minutes-long 0DTE trade
can be run from the same terminal as a swing: sub-minute chart bars, an
entry/exit **spread optimizer** you can switch on and off, and named
**risk profiles** (DEFAULT / SCALP / SWING) that set every server-enforced
rule in one tap. Nothing changes for a plan placed before this: the
optimizer is OFF by default and legacy plans keep the legacy exit ladder.

## 1. Sub-minute bars (5s / 15s / 30s)

The chart's timeframe rail now starts at **5S 15S 30S** before 1M. These
bars are not broker bars — Alpaca streams nothing faster than 1 minute —
they are rolled in-process from the **trade tape** (`services/fast_bars.py`)
for the symbol on screen:

- Subscribing a fast timeframe adds a `subscribe_trades` reference for
  that symbol (`market.subscribe_fast`); releasing the last one drops the
  tape subscription and the series. The 1m bars and quotes are untouched.
- On first subscription the last 45 minutes of prints are seeded from
  REST (real-time on the feed you have: IEX on the free tier, whose REST
  trades are live — only SIP is 15 minutes delayed), then the stream
  extends them. Three RTH hours are kept per symbol, in memory only.
- Prints whose conditions mark them as not price-forming (average price,
  out of sequence, derivatively priced, bunched, official open/close…)
  are dropped — the same set the 1m bars exclude.

**Read the volume honestly.** On the free tier the tape is IEX-only:
prices are real prints, volume is a few percent of the consolidated
market (near zero in thin names). A bucket with no print is absent, not
flat — the chart compresses the gap. Nothing in the engine prices off
these bars: the risk gate, the enforcer and every strategy still read 1m
bars and quotes. The fast bars are sight for a human, not an input.

The axis switches to HH:MM:SS below 1m. RSI/MACD/VWAP/ATR all work on
any timeframe; the realized-vol and "suggested stop" numbers that scale
by bar length get noisy on 5s bars (as they already were on 1m).

## 2. The spread optimizer

Off, the engine does what it always did: an entry rests **at the mid**
until the entry TTL cancels it; an exit ladders **mid −2% → mid −6% →
market** with five seconds per rung. On a penny-wide 0DTE SPY book the
2% rung is already through the bid; on a $0.30-wide wing it never reaches
it. Both are the wrong unit.

On, everything is priced in **position half-spreads** of the live book
(sum over legs of |weight| × leg half-spread — the same number the fair
value filter uses), on the signed position-value axis where "marketable"
is UP for entries and DOWN for exits, so one rule covers every structure
including credits:

| | rung 0 | then | last limit | then |
|---|---|---|---|---|
| **entry** | `mid + start·hs` (default the mid) | `+step·hs` every `step_s` | `mid + max·hs` (default the touch) | rests until the entry TTL |
| **exit** | `mid − 0.25·max·hs` | `−0.5·max·hs` | `mid − max·hs` (default the bid) | market |

Rounding is always toward the marketable side of the tick so a rung
never quietly lands on the wrong side of a cent.

Entry mechanics (`TradeService.rework_entry`, driven by the enforcer's
monitor at `step_s` cadence): stamp `pricing.reworking` on the plan,
cancel the resting rung, confirm the cancel came back **empty** (a fill
that beat the cancel is the entry filling — the chase stops; a partial
is managed as a partial), reprice the next rung off the live book,
submit it under a fresh idempotency key (`{plan}-e{rung}`), swap the
plan's entry order. The broker's own cancel event for a rung the chase
replaced is recognised (stream and reconcile) and ignored. The chase
never follows the market past the tolerance `place_trade` applied to the
staged price (max(2×hs, 8% of |mid|)): beyond that it cancels and notes
"chase abandoned" — you re-stage off fresh numbers. The entry TTL is the
hard ceiling either way.

Where the choice lives:

- **Global**: risk setting `spread_optimizer` plus the five dials
  (`spread_opt_step_s`, `spread_opt_entry_start/step/max`,
  `spread_opt_exit_max`) — ACCOUNT tab on the desktop drawer, ACCOUNT tab
  on the phone.
- **Per order**: the ticket's WORK SPREAD chips (AUTO / ON / OFF) send
  `work_spread` on the order; the server stamps the choice on
  `trade_plans.pricing` (migration 0010) so the plan's exit ladder follows
  the same choice regardless of what the global setting is later.
- Legacy plans (`pricing` NULL) follow the global setting at exit time.

The execution-quality ledger (`exec_quality.spread_capture`) is the
scorecard: 1.0 = filled at the fair value, 0.0 = paid the whole
half-spread. Compare it ON vs OFF over a few dozen fills before trusting
either mode with size.

## 3. Profiles

`GET /api/settings/risk/presets` lists them; `POST
/api/settings/risk/preset/{name}` applies one (risk rules, then the feed
cadences it carries). The PROFILE chips on both ACCOUNT tabs call these;
the lit chip is whichever preset the stored settings currently *equal* —
a hand edit simply un-lights it. Capability facts (options level, shorts)
are never in a preset.

| | DEFAULT | SCALP | SWING |
|---|---|---|---|
| max loss / trade | 2% | **1%** | 2% |
| daily loss breaker | 6% | **3%** | 6% |
| max positions | 3 | 2 | 4 |
| default TP / SL | +100% / −50% | **+30% / −20%** | +100% / −50% |
| entry TTL | 5m | **1m** | 10m |
| max trades / day | 20 | 40 | 10 |
| max leg spread | 15% | **10%** | 15% |
| SL confirm dwell | 3s | **1s** | 3s |
| time stop / expiry-day stop | 15:50 / 15:15 | 15:55 / 15:50 | 15:50 / 15:15 |
| spread optimizer | off | **on, 2s rungs, mid → ask** | on, 5s rungs, starts −½ hs inside |
| chain refresh / positions poll | 10s / 5s | **2s / 2s** | 15s / 5s |

The SCALP numbers are a starting point for the style, not a study result:
on a ~$10k cash account each shot risks ~$100, three losers end the day,
and the whole trade is expected to be over inside the one-minute entry
shelf life plus a HOLD chip (+5/+10/+20/+45 minutes on the ticket set the
time stop from now).

Two account facts that matter for scalping on the Roth:

- **PDT is a margin rule.** The risk gate's "4th day trade under $25k"
  refusal now checks the broker's multiplier and stands down on a cash
  account. What a cash account *does* have is T+1 settlement on option
  proceeds — the broker, not the engine, enforces good-faith rules, so
  the settled-cash line on the ACCOUNT tab is the real per-day budget.
- **Level 2 stays level 2.** Long single-leg only on the live server;
  the SCALP profile does not change that.

## 4. What is deliberately not here

- No option-quote WebSocket to the browser yet: the ticket's bid/ask
  still arrive via the chain snapshot (the SCALP profile drops that to 2s;
  the server-side chain cache is 5s). Live per-leg streaming to the ticket
  is the next step if 2s proves too slow on the screen.
- The exit ladder's optimizer rungs apply to manual closes, time stops
  and stop losses alike; the broker-resting take-profit is unchanged (a
  limit at the TP price is already the best possible exit).
- Sub-minute bars are not persisted and not backfilled beyond 45 minutes.
