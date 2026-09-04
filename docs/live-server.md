# The live server: an isolated mirror of the engine, on an always-on Linux box, over Tailscale

This is the runbook for standing up planetaria's **live server** — the
process that touches real money — on a dedicated 24/7 Linux machine and
reaching it from anywhere through Tailscale. It is written so that a
Claude Code session **on the Linux box** can execute it top to bottom and
then prove the result with the tests in §5. The regular-hours smoke test
in §6 is done by a human clicking the UI.

## 0. What the live server is (and is not)

Same codebase, same engine, booted in `TRADING_MODE=live_manual`:

| | paper server | live server |
|---|---|---|
| broker endpoint | paper-api (`ALPACA_PAPER=true`, refuses false) | live (derived; `ALPACA_PAPER` ignored) |
| account pool | `PK…` keys only, `AK…` dropped | `live_*`-named `AK…` keys only, everything else dropped, pinned by `LIVE_ACCOUNT_NAME`, no fallback |
| strategy plane (bus, feeds, runner, breakers) | constructed | **never constructed** (`bootstrap.strategy_plane_enabled`) |
| `/api/strategies`, `/api/signals` | real router | 409 stub |
| entry orders | human ticket **and** `ctx.submit()` from strategies | human ticket only; `strategy_id` refused; options long single-leg only (level 2) |
| exit enforcer (TP/SL/time-stop/reconcile) | full | **full** — protective stops on live positions are the point |
| adopt broker positions into managed plans | options | options **and** shares (floor qty) |
| database / redis | `trader` / db 0 | `trader_live` / db 1 (boot refuses the paper defaults) |
| bind | `0.0.0.0` (LAN phone access, paper money) | `127.0.0.1` + `tailscale serve` |

Every difference is a **boot refusal**, not a runtime flag: a misconfigured
live process dies rather than degrades (`config.validate_paper_lock`,
`AccountService.apply`, `bootstrap.startup`). The paper server's own
locks are byte-for-byte what they were.

**The one operational rule: exactly one live process per account, ever.**
Two enforcers on the same account (the Windows `planetaria-live` service
*and* the Linux box, say) each own a copy of the plans and would both try
to close the same position. Before the Linux box goes live, the Windows
live service must be stopped and disabled (`nssm stop planetaria-live;
nssm set planetaria-live Start SERVICE_DISABLED`) — or never installed.

## 1. Why this shape

- **Isolation beats a flag.** A bug in a strategy, a feed or the breaker
  loop cannot reach the live account because those objects do not exist
  in the live process. That is a stronger guarantee than any `if live:`.
- **An always-on box beats a desktop.** The stop-loss is software-
  enforced (no broker stop orders for options). A machine that sleeps,
  reboots for updates, or gets its Claude session closed is an
  enforcement outage. A low-power laptop with the lid shut, no sleep,
  systemd `Restart=always`, is the right host for an enforcer.
- **Tailscale answers the "no auth" problem.** The API has no
  authentication. On the paper server that is acceptable on a home LAN;
  for real money it is not. Binding to loopback and publishing through
  `tailscale serve` means only devices logged into *your* tailnet can
  reach it, over WireGuard, with HTTPS from Tailscale's own certs. No
  port forwarding, no public surface. (`tailscale funnel` would make it
  public — never enable it.)

Known Linux difference: the in-app `KeepAwake` (Windows
`SetThreadExecutionState`) reports "unsupported on this OS". That is
expected — sleep is disabled at the OS level in §2 instead.

## 2. Setup (the Linux session runs this)

Prereqs: Debian/Ubuntu, sudo, a normal login user (the service runs as
that user), the repo cloned to `~/planetaria`, the two live keys at hand.

```bash
cd ~/planetaria
bash deploy/live/setup-linux.sh
```

The script is idempotent and does: apt packages (python3-venv, native
postgresql + redis-server — no Docker daemon on a low-power box), the
`trader` role and `trader_live` database, the backend venv from
`requirements.lock.txt`, the frontend build (the backend serves
`frontend/dist` itself, so no node process at runtime), the env file,
the systemd unit (enabled, not started), no-sleep logind config, and the
Tailscale install. It ends by printing the manual steps:

1. `sudo nano /etc/planetaria/live.env` — paste `ALPACA_ACCOUNT_LIVE_ROTH_API_KEY`
   / `_SECRET_KEY`. There is deliberately **no `.env`** in the checkout
   on this box: no paper keys, no LLM keys, nothing the live process does
   not need.
2. `sudo systemctl start planetaria-live` then
   `curl -s http://127.0.0.1:8001/api/health` →
   `"mode":"live_manual","paper":false,"alpaca_keys_configured":true`.
3. `sudo tailscale up` (browser login), then name the node and publish
   the server on both a typeable plain-HTTP name and an HTTPS name (the
   tailnet is WireGuard-encrypted, so HTTP inside it is fine; the phone's
   "Add to Home screen" needs the HTTPS one):
   `sudo tailscale set --operator=$USER --hostname planetaria`,
   `tailscale serve --bg --http=80 http://127.0.0.1:8001`,
   `tailscale serve --bg --https=443 http://127.0.0.1:8001`.
   NOTE: `tailscale serve` config is keyed on the node's DNS name — after a
   rename, `tailscale serve reset` and re-add, or every request 404s.
   Result: **`http://planetaria`** from any tailnet device, and
   `https://planetaria.tail6d5ddc.ts.net` for the phone app.
4. From the Windows box (also on the tailnet):
   `http://planetaria/terminal` — the header badge must
   read **LIVE in red**. (`/terminal.html` still works; `/terminal` is the
   clean path the backend serves for it.)
   On the live server the ROOT `http://planetaria/` is the
   ACCOUNT OVERVIEW — equity big, today's move, holdings sortable by
   SIZE / EXPOSURE / MOVERS / P/L with each row's protection (STOP or NO
   STOP) and a tap into that symbol's trading interface. The ops console
   stays at `/index.html`.
5. **Phone.** Open the same URL in mobile Chrome on the tailnet, then Chrome
   menu → *Add to Home screen*: the manifest installs the terminal as a
   full-screen app (no browser bars over the chart). Under 640px the
   terminal switches to its phone shell — chart home with pinch-zoom, the
   book docked under it, one TRADE button, POSITIONS / ACCOUNT / MORE tabs.
   Everything that moves money there is a two-tap (confirm strip).

Optional but recommended in the Tailscale admin console: an ACL that
limits port 443 on this node to your own devices (tag it `tag:live` and
grant only your user), and key expiry disabled for the node so a lapsed
login cannot take the enforcer's remote access down.

Updating the code later: `git pull`, `.venv/bin/pip install -r
requirements.lock.txt`, `cd frontend && npm ci && npm run build`, `sudo
systemctl restart planetaria-live` — **outside** 09:25–10:00 and
15:45–16:05 ET, when the enforcer may be mid-exit.

## 2b. Auto-deploy outside trading hours

The box keeps itself on the latest *working* commit without anyone
touching it during the session. `planetaria-deploy.timer` runs
`deploy/live/autodeploy.sh` every 15 minutes; the script:

1. **Refuses to act during the trading day** — it only proceeds
   20:30–07:30 ET on weekdays, or any time Saturday/Sunday (the engine
   may be mid-exit 09:25–10:00 / 15:45–16:05 and extended-hours polling
   runs 04:00–20:00). A `~/planetaria/.deploy-hold` file pauses it.
2. Fetches `origin/main`; exits if the box is already there, or if the
   checkout is dirty or not fast-forwardable (a human made local changes —
   never silently overwritten).
3. Fast-forwards, reinstalls Python deps / npm packages only when their
   lock files changed, then **runs the full backend suite on the box** —
   that is the definition of "working", not the commit message.
4. Rebuilds the UI, restarts the service, checks `/api/health` says
   `live_manual`.
5. On any failure — tests, build, health — **rolls back** to the commit that
   was running, rebuilds, restarts, and logs `ROLLBACK`. The log is
   `~/planetaria-deploy.log` (pytest output in `~/planetaria-deploy.log.pytest`).

Install once (needs sudo; uses the scoped NOPASSWD rule for the restart):

```bash
sudo bash ~/planetaria/deploy/live/install-autodeploy.sh
```

Check it: `systemctl list-timers planetaria-deploy.timer`, and
`tail ~/planetaria-deploy.log` shows `skip: inside trading day` during the
session and `deployed <sha>, healthy` at night. Dry-run any time:
`bash deploy/live/autodeploy.sh --dry-run`.

What it does NOT do: deploy anything that isn't on `origin/main` (push is
the human act that releases), touch `/etc/planetaria/live.env`, or run
migrations by hand (the app auto-migrates at boot).

## 3. Files

- `deploy/live/setup-linux.sh` — the setup above.
- `deploy/live/planetaria-live.service` — systemd unit (`__USER__`/`__REPO__` substituted by the script).
- `deploy/live/live.env.example` — the whole environment of the live process.
- `live.ps1` / `backend/scripts/install-live-service.ps1` — the Windows
  equivalents for a side-by-side session on the dev box (same contract,
  Docker infra on :5433/:6380). Only one of the two hosts may run live.

### Extended hours are not an outage

The free data tier's IEX stream runs 08:00–17:00 ET only. Before and after
it, an `extended-hours-poll` task refreshes every quoted equity every 15 s
through `ah_quote()` (broker REST latest quote first, then Yahoo/Finnhub
prints), so the header, marks, P/L and the ticket keep moving; overnight
the Blue Ocean poller does the same. When the quote cache has nothing
usable (weekend), `/api/positions` marks off the broker's own position
prices (`mark_source: "broker"`, shown as BRK) instead of STALE, and the
equity ticket turns extended-hours on automatically outside RTH (DAY limits
without it just queue to the open). Exits are unchanged: the enforcer
marks off its own quote map.

## 4. Risk posture for a ~$11k cash account (seed once, via the UI risk panel or `PUT /api/settings/risk` on :8001)

| setting | value | why |
|---|---|---|
| `max_loss_pct` | 0.02 (~$220/trade) | default |
| `daily_loss_pct` | 0.03 | tighter than the paper default |
| `max_positions` | 6 | 4 adopted ETFs + 2 discretionary |
| `equity_max_notional_per_name_pct` | 0.10 | new entries only; adoption bypasses entry validation by design |
| `equity_gross_exposure_pct` | 1.0 | the adopted ETFs are ~78% invested and the gross gate counts every open equity plan — the 0.50 default would block all new entries. 1.0 is structurally safe on a cash account (no margin; Alpaca enforces settled cash) |
| `max_trades_per_day` | 5 | |
| `equity_long_only`, `manual_equity_require_stop` | true | keep; on the live server every entry is discretionary, so the stop requirement binds on all of them |
| `options_level` | 2 | ACCOUNT CAPABILITIES (ACCOUNT page): the Roth is options level 2. The UI then offers only long single-leg presets / no sell buttons on the chain / no theta templates, and the risk gate refuses anything else. The live server floors this at 2 whatever is stored |

## 5. Verification tests (the Linux session runs these; all must pass before §6)

```bash
cd ~/planetaria/backend

# 5.1 the suite, including the live-mode contract
.venv/bin/python -m pytest -q                       # expect all green
.venv/bin/python -m pytest -q tests/test_live_mode.py tests/test_accounts.py tests/test_adopt.py

# 5.2 negative boots: each must print a RuntimeError, never start
set -a; . /etc/planetaria/live.env; set +a
STRATEGIES_ENABLED=true  .venv/bin/python -c "from app.config import get_settings; get_settings()"
LIVE_ACCOUNT_NAME=       .venv/bin/python -c "from app.config import get_settings; get_settings()"
DATABASE_URL=postgresql+asyncpg://trader:trader@localhost:5433/trader .venv/bin/python -c "from app.config import get_settings; get_settings()"
# and the paper lock is still intact:
TRADING_MODE=paper ALPACA_PAPER=false .venv/bin/python -c "from app.config import get_settings; get_settings()"
# and a live boot without its keys dies (no fallback): temporarily unset them
ALPACA_ACCOUNT_LIVE_ROTH_API_KEY= .venv/bin/python -c "
import asyncio
from app.config import get_settings
from app.services.system_state import AccountService
class DB: pass
asyncio.run(AccountService(DB(), get_settings()).apply())"     # expect RuntimeError: refusing to boot

# 5.3 the running service
systemctl is-active planetaria-live                          # active
curl -s 127.0.0.1:8001/api/health                            # mode live_manual, paper false, keys configured
curl -s 127.0.0.1:8001/api/system/state | python3 -c "
import json,sys; s=json.load(sys.stdin)
print('broker', s['broker']); print('tasks', s['tasks'])
assert s['broker']['mode']=='live_manual' and s['broker']['paper'] is False
assert 'strategy_runner' not in s or not s.get('strategies')   # no strategy plane
assert s['tasks']['trading_stream']=='running' and s['tasks']['reconcile_loop']=='running'
print('OK')"
curl -s 127.0.0.1:8001/api/account            # the Roth's REAL equity/cash; "mode":"live_manual"
curl -s 127.0.0.1:8001/api/positions          # the four ETFs under "untracked"
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8001/api/strategies          # 409
curl -s -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:8001/api/strategies/x/enable   # 409
curl -s 127.0.0.1:8001/api/system/accounts    # {"paper_only": false, "mode": "live_manual", one account, ACTIVE}

# 5.4 the entry gates answer at the HTTP edge (no order is placed: both are refused before the broker)
curl -s -X POST 127.0.0.1:8001/api/orders -H 'content-type: application/json' -d '{
 "underlying":"SPY","strategy":"long_call","strategy_id":"pead-1","legs":[{"symbol":"SPY261218C00600000",
 "right":"C","strike":600,"expiry":"2026-12-18","side":1,"ratio":1,"entry":1.0,"iv":0.2}],
 "qty":1,"entry_limit":1.0,"tp_premium":2.0,"sl_premium":0.5,"time_stop_utc":"2026-12-01T20:00:00Z"}'
#   -> 422 "automation ids are not accepted on the live server"
# same body with two legs (add a side:-1 leg) -> 422 "options level 2"

# 5.5 remote reach (from the Windows box, on the tailnet)
#   curl https://<box>.<tailnet>.ts.net/api/health    -> same JSON as 5.3
#   and the terminal page renders with the red LIVE badge; the strategies
#   page shows the LIVE SERVER lock; the SYSTEM drawer shows BROKER
#   "ACTIVE · LIVE (manual only)", EXIT ENFORCER monitors, RECONCILE running.

# 5.6 restart safety: the enforcer rebuilds from the DB
sudo systemctl restart planetaria-live && sleep 8 && curl -s 127.0.0.1:8001/api/system/state | grep -o '"monitors":[0-9]*'
journalctl -u planetaria-live --since -2min | grep -E "LIVE alpaca account|strategy plane NOT constructed|startup reconcile complete"
```

## 6. Regular-hours smoke test (a human clicks; the box only enforces)

Market hours 09:30–16:00 ET. Use whole shares (`qty` is integral; the
account has fractional trading on but the ticket does not send notional
orders). Cash account: buying with settled cash and selling the same day
is fine; do not re-deploy *same-day sale proceeds* into another
same-day round trip (that is the good-faith-violation shape, T+1).

1. **Adopt the four ETFs** (positions → untracked → adopt) with an explicit
   `sl_pct` (≈0.10 of the share basis) and a far `time_stop_utc` (+30d).
   Result: four `filled` equity plans, four monitors in SYSTEM, real
   stops under leveraged products for the first time. Fractional
   residues (if any) stay untracked — close them by hand at Alpaca.
2. **One tiny equity round trip**: buy 1 share of a liquid ETF from the
   equity ticket with a stop → the button reads `BUY 1 XYZ (LIVE)` in
   red → the confirm strip says `LIVE ORDER — REAL MONEY` → CONFIRM →
   watch the fill land, the plan go `filled`, the monitor arm, the TP
   rest at the broker (if a target was set) → manual close from the
   positions drawer → `closed`, realized P/L recorded.
3. **Options level 2**: try a 2-leg ticket first — the panel shows the
   "options level 2 — long single-leg only" warning and the LIVE button
   stays disabled (and the server would 422 it anyway). Then one cheap,
   near-dated long single-leg contract → red `CONFIRM LIVE ORDER — REAL
   MONEY` overlay → fill → manual close.
4. **Exit enforcement for real**: on a filled plan, tighten the stop
   (PATCH exits) to just under the mark and watch the enforcer close it
   through the normal ladder; check the lifecycle journal
   (`/api/positions/{id}/events`) reads like the paper ones.
5. Gotchas: the PDT guard can false-positive on a cash account
   (`daytrade_count` ≥ 3 blocks entries; harmless, cash accounts are
   PDT-exempt); never raw-cancel an order whose client id ends in
   `-xtp…` (that is the resting take-profit — tighten instead); do exits
   in RTH so they do not park against a closed market.

After the smoke test, everything the paper server has learned about
execution quality (`exec_quality` on every plan, spread capture per
fill) is now being measured on the live book too — compare them.

## 7. Two-server hygiene (the rules, now that there are two)

The whole point of the split is that the two processes cannot hurt each
other. That holds by construction inside the code; these are the human
rules that keep it holding outside the code.

**One live process per account — enforced, not just remembered.**
`live.ps1` on Windows probes the Linux box's health endpoint
(`LIVE_PEER_URL`, default the box's MagicDNS name) and refuses to boot a
second live server while the peer answers `mode=live_manual`; `-Force`
overrides only after you have stopped the peer. The Linux box is the
live host of record; the Windows live service exists for side-by-side
testing and should stay **uninstalled** (or `SERVICE_DISABLED`) while the
box runs. Two enforcers on one account = both try to close the same
position.

**Secrets live in exactly one place per host.**
- Linux box: `/etc/planetaria/live.env` (root:user, 0640). No `.env` in
  the checkout. No paper keys, no LLM keys, nothing the live process does
  not need.
- Windows: `.env` (gitignored, both paths). Once the box is the live
  host, **remove the `ALPACA_ACCOUNT_LIVE_ROTH_*` lines from the Windows
  `.env`** — the paper registry drops them anyway, but a key that is not
  on the machine cannot leak from it.
- Never paste a password, key or token into a chat, a ticket, a commit
  or a doc. If one has been pasted anywhere, rotate it. Machine access is
  key-based SSH or Tailscale SSH (`tailscale up --ssh` on the box; identity
  is the tailnet login, no password ever typed).
- Rotate the live API key from the Alpaca dashboard on a calendar
  (quarterly), and immediately if a machine that held it is lost.

**Network surfaces.**
- Live: `127.0.0.1:8001` + `tailscale serve`. Never `--host 0.0.0.0`,
  never `tailscale funnel`, no router port-forward.
- Paper: `0.0.0.0:8000` on the LAN for the phone. Acceptable for paper
  money; it should also move behind `tailscale serve` when convenient so
  there is one access model, not two.
- Tailscale admin: disable key expiry on `planetaria` (was mikoyae-kojiki) (a lapsed node
  key takes the enforcer's remote access down silently); ACL the box to
  your own user; keep the tailnet single-user.

**Deploy discipline on the box.**
- The checkout on the box is read-only in spirit: `git pull` only, never
  local edits, never a branch. Fix on Windows, test with `check.ps1`,
  commit, push, pull.
- Restart outside 09:25–10:00 and 15:45–16:05 ET.
- After every pull: the §5 verification block. After every restart:
  `journalctl -u planetaria-live --since -2min` shows `LIVE alpaca account
  'live_roth' pinned`, `strategy plane NOT constructed`, `startup
  reconcile complete`.

**State is per server and never shared.** `trader_live` and redis db 1
belong to the live process; `trader` and db 0 to paper. No cross-reads,
no shared dumps, no copying a plans table from one to the other. The
paper server's portfolio view will get a *read-only* live row later; it
will read the broker, not the live DB.

**Time and power.** NTP on (`timedatectl set-ntp true`); sleep masked;
lid switch ignored. A box that drifts or naps is an enforcement outage,
and the app can only tell you afterwards.

## 8. What is deliberately not here yet

- Phone access to live goes through the Tailscale app on the phone (same
  URL); there is still no per-request auth on the API itself. Anyone
  who can log into your tailnet can trade — keep it single-user.
- The paper server's portfolio view does not show the live account.
  Read-only live rows on the paper monitor are a small follow-up
  (`portfolio_accounts.py` already derives paper/live from the key
  prefix; only the registry gate keeps AK keys out of the paper process).
- Fractional entries, and a stop on the fractional residue of an
  adopted position.
