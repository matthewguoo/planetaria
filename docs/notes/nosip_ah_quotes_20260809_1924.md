# no-SIP AH data/trading asymmetry — verified 2026-08-09 19:24 ET

equity session: **closed (weekend gap)**; configured feed: **iex**

| symbol | source | price | age | note |
|---|---|---|---|---|
| SPY | iex latest | 385.82 | 51.4h | the entitled feed |
| SPY | sip latest | — | — | HTTP 403: subscription does not permit querying recent SIP data |
| SPY | sip 16m ago | — | — | no quotes in window |
| SPY | sip 5m ago | — | — | HTTP 403: subscription does not permit querying recent SIP data |
| SPY | iex @ Fri 17:30 ET | — | — | 0 NBBO updates in that minute |
| SPY | sip @ Fri 17:30 ET | 772.98 | 49.9h | 4 NBBO updates in that minute |
| SPY | yahoo | 773.40 | 47.4h | public print — the entry-pricing source |

order path: 1 SPY extended-hours DAY limit @ 386.58 (50% under last close) -> status **accepted** (id 3fefb3b3-2471…) — accepted while the entitled quote is stale
cancelled -> status canceled. No position, no fill, no residue.

Reading: `iex latest` fresh only 08:00-17:00 ET; `sip latest` refused (the paywall); `sip 16m ago` returned (the free audit channel pead_nosip verifies fills against); `sip 5m ago` refused (the 15-minute boundary); the public print is what prices the entry. Execution and data are separate products — the order path stays open through all of it.
