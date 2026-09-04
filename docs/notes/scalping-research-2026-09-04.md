# What options scalping actually needs — and where the terminal stands

Research note, 2026-09-04. Prompted by r/options "Options 'scalping'"
(u/714trader, Dec 2023, thread pasted into the session — Reddit blocks
fetching). Secondary sources: my0dteoptions.com "Best platforms for
scalping 0DTE options" (2026), quantvps.com "Top 5 SPY options scalping
strategies", Alpaca docs/blog (market-data plans, index options launch
2026-09-02, unsettled funds). Companion to `docs/scalping.md` (what
shipped on 2026-09-04).

## 1. What the thread says (primary source, condensed)

The OP's problem is not signal, it is **click latency**: "it takes me
2-3 min just to input an option trade" from a generic brokerage UI.
Every substantive answer is about the order path, not the strategy:

| Point | Who | Takeaway |
|---|---|---|
| Trade from a **price ladder / DOM**, one click per order, count of open orders on screen | starbolin, tutoredstatue95, [deleted] | TOS Active Trader, tastytrade: "in and out of positions in seconds" |
| **Saved order + auto-fill from last order** → subsequent orders are one click | starbolin | The "repeat last" pattern |
| **Hotkeys + bracket auto-exit**: "spot signal, hit hotkey to enter, set auto exit with bracket order. no extra clicks" | Alternative-Task4168 | Bracket = what our enforcer already is |
| **DMA brokers fill faster than PFOF** (Lightspeed, IBKR vs Webull/RH/TOS): "you're gonna get slipped bad" | Mrtoad88 | Routing/venue quality matters at this horizon |
| **Liquidity exists only in ~10 macro names**; "lack of liquidity, lack of data flow and market microstructure" is the real problem | radio_chemist, starbolin | SPY/QQQ; the spread gate is the right gate |
| **The spread is the cost**: "you're only paying fees to your broker and the implied volatility bid ask spread… just don't do that" | sharpetwo | Spread capture is the scorecard |
| **If you want speed, quantify and automate through the API** — "executions will always be sub par when done by a human" | tutoredstatue95 | planetaria's strategy runtime is that path |
| **A chart of the option's own price** to trade from ("AAPL chart but… $4.00 option price") | 714trader (idea) | Not offered by the answers; buildable here |
| Monitor the close window with the limit arrows; set TP/SL % so you can go AFK | Clutch_Mav | Server-side brackets, which we have |
| Cheap butterflies / iron flies when daily IV is high, 1:1 risk, managed at 10-20% | agoodgai | Matches the fly study — the scalp that pays is premium |
| **"Nobody should be scalping in an IRA"** — losses above the contribution limit cannot be replaced, and are not deductible | PapaCharlie9 (mod) | Fact worth keeping in view for the Roth |
| Tastytrade offers one-click but "advises against it… not something you want to misclick on" | Other-Inspector-9116 | One-click must be armed deliberately |

## 2. What the practitioner articles add (numbers)

- Execution targets: market orders on SPY 0DTE "fill in under 500 ms";
  a limit at the mid "should either fill or ping within 1 second".
  Two-handed key combos or a mouse confirm "add 500-1000 ms per action".
- Data: Level 2 per strike ("where liquidity is sitting"), time & sales
  ("real flow or a wholesaler"), real-time greeks. Note: Alpaca offers
  **no options depth at all**, only NBBO quotes + trades via OPRA.
- Hotkeys, single keystroke each: buy-ask, sell-bid, flatten, cancel-all,
  bracket with preset stop/target.
- Fees: $0.65/contract retail vs $0.15 at volume; "50 round-trips at
  $0.65 = $6,500/month". Alpaca: equity options commission-free (pass-
  through fees only); index options $0.50/contract.
- Strategy mechanics that recur: 1m candles for opening-range breaks
  (09:30-11:00), 5m/15m + 20 EMA for trend pullbacks (10:00-11:30,
  14:00-15:30); targets **15-40% of premium**, stops **10-20%**, holds
  minutes to an hour; risk **0.5-2% per trade**, max 3-4 open; avoid IV
  above its 80th percentile and the minutes around Fed/CPI/NFP; "wait 2-3
  minutes for confirmation" on breaks. The SCALP profile's +30% / −20% /
  1% / 2 positions sits inside these bands.

## 3. Where the terminal stands

| Need | Status | Notes |
|---|---|---|
| Server-enforced bracket (TP/SL/time) that survives the UI | **have** | ExitEnforcer; resting TP at broker |
| Spread worked instead of paid | **have** (2026-09-04) | per-order toggle, half-spread ladder both ways |
| Sub-minute chart | **have** (2026-09-04) | 5s/15s/30s from the tape (IEX prints on free tier) |
| Profiles with scalp-sized rules | **have** | DEFAULT / SCALP / SWING |
| Two-tap close / flatten on the phone | **have** | |
| Execution scorecard | **have** | `exec_quality.spread_capture` per fill |
| One-click armed order entry, hotkeys, repeat-last | **missing** | ticket is pick → confirm overlay → submit |
| Trade from a contract price ladder | **missing** | NBBO-only ladder is buildable; no L2 exists on Alpaca |
| Live option quotes in the browser | **missing** | ticket reads the chain snapshot (2s under SCALP; server cache 5s) |
| Option-premium chart (the OP's idea) | **missing** | needs real-time OPRA trades → Algo Trader Plus |
| Time & sales | **missing** | underlying prints already stream server-side |
| Order latency ledger (submit→ack→fill ms) | **partial** | admin vitals has broker latency; per-order stamps absent |
| Liquidity gate beyond spread (OI / volume) | **missing** | spread gate only (10% under SCALP, $0.05 abs floor) |
| Settled-cash budget on a cash account | **missing** | Roth is multiplier 1; Alpaca's float-cover statement covers margin/limited-margin only |
| SPX / XSP (cash-settled, no assignment, 1256) | **not usable yet** | Alpaca live since 2026-09-02 ($0.50/contract) but "does not currently provide index data" — no quotes means no enforcer |
| Full OPRA + SIP real-time, 1000 option symbols | **missing** | Algo Trader Plus, $99/mo (also the PEAD/SIP queue item) |

## 4. What we need, in order

1. **One-click armed ticket + hotkeys (small).** An ARM toggle with a
   loud state (red on live) that skips the confirm overlay for the next
   N minutes; keys: `B` buy the staged structure, `C` close last plan,
   `F` flatten, `X` cancel working entries, `R` repeat last order at
   the fresh mid, `+`/`-` step the contract. The thread's whole point.
   Misclick defence: ARM auto-disarms after N minutes and on symbol
   change; every hotkey order still goes through place_trade's gates.
2. **Live option quotes to the browser (small-medium).** WebSocket
   `oquote` channel (server already publishes `oquote:{sym}` internally
   for the enforcer). The ticket, the position rows and the chart's
   premium readout stop waiting on the chain poll. Prerequisite for 3-4.
3. **Contract ladder panel (medium).** For the staged contract: NBBO
   bid/ask with sizes, last, the working entry rung, click-a-price to
   set the limit, drag to reprice (feeds `rework_entry` as a manual
   rung). Honest label: this is NBBO, not depth — Alpaca has no L2.
4. **Option-premium chart + time & sales (medium).** Roll 5s/15s/1m
   bars of the CONTRACT from OPRA trades (same `fast_bars` machinery,
   keyed by OCC symbol) and show a T&S tape for contract and underlying.
   Needs Algo Trader Plus: the free `indicative` feed is quotes-only and
   capped at 200 symbols. This is the OP's "chart with option prices".
5. **Order latency ledger (small).** Stamp submit→accepted→fill on every
   order into `exec_quality`; surface p50/p95 in admin vitals. The
   articles' "<500 ms market / 1 s limit ping" becomes a measured fact
   about Alpaca + this box instead of an assumption.
6. **Liquidity gate v2 (small).** Refuse contracts under an open-interest
   / day-volume floor (chain snapshot carries both) in addition to the
   spread cap — the thread's "only ~10 names have the liquidity".
7. **Settled-cash budget (small-medium, Roth-specific).** Track the day's
   opening buys against settled cash and warn before a round trip that
   would be a good-faith violation; then VERIFY Alpaca's IRA behaviour
   with one small same-day round trip and the next-day `cash` /
   `non_marginable_buying_power` readings, since their docs only
   describe margin and limited-margin float cover. Three GFVs in twelve
   months = 90 days settled-cash-only.
8. **Algo Trader Plus ($99/mo).** Unlocks 4, real-time SIP for the
   equity tape (fast bars stop being IEX-only), and the standing PEAD
   after-hours sight problem. One subscription answers three queues.
9. **Index options: watch, don't build.** SPX/SPXW/XSP live on Alpaca as
   of 2026-09-02 (cash-settled, European, Section 1256, AM-settled
   cutoffs), but with no index market data the enforcer cannot price a
   stop. Revisit when Alpaca ships index quotes.
10. **Automate the scalp (the thread's real answer).** The strategy
    runtime exists; what is missing is an edge that survives our
    latency. The 2026-08-10 open-window study found no 15-30 minute
    opening edge for a long-only account and ignition longs dead under
    every stop; the 0DTE premium sleeve (QQQ afternoon fly) is the scalp
    that measured positive. A discretionary scalp on 5s bars is a human
    edge claim; log it through `exec_quality` and the twin before
    believing it.

## 5. Two facts to keep in front of the trader

- The mod's IRA point is arithmetic, not opinion: a $1,000 loss in a
  Roth is a $1,000 permanent reduction of tax-free compounding capacity,
  and it cannot be deducted. Whether to scalp in it is Matthew's call;
  the terminal's job is to make the SCALP profile's 1% / 3% stops bind.
- On options the spread is the fee. `spread_capture` over the first few
  dozen fills, optimizer ON vs OFF, is the only number that says whether
  scalping here is paying the market maker or being paid.

Sources: the pasted thread; https://my0dteoptions.com/blog/best-platforms-scalping-0dte-options/ ;
https://www.quantvps.com/blog/top-5-spy-options-scalping-strategies ;
https://docs.alpaca.markets/docs/about-market-data-api ;
https://alpaca.markets/blog/alpaca-launches-index-options-via-trading-api/ ;
https://alpaca.markets/learn/understanding-unsettled-funds ;
https://alpaca.markets/support/what-are-the-commission-fees-per-option-contract
