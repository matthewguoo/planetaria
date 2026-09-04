# The paper server on the box: second checkout, second unit, same engine

The paper engine moves from the Windows desktop (NSSM `planetaria-engine`,
`0.0.0.0:8000`, SQLite fallback store) onto the Linux box that already runs
the live server. Same codebase; a second clone, a second systemd unit, its
own Postgres database and Redis index, reached at **`http://planetaria:8000`**
over the tailnet. Nothing about the live server's isolation changes: the two
processes share a machine and nothing else.

## 0. Shape

| | live (`~/planetaria`) | paper (`~/planetaria-paper`) |
|---|---|---|
| unit | `planetaria-live` | `planetaria-paper` |
| bind / URL | `127.0.0.1:8001` → `http://planetaria` | `127.0.0.1:8000` → `http://planetaria:8000` |
| env | `/etc/planetaria/live.env` | `/etc/planetaria/paper.env` |
| store | Postgres `trader_live`, redis db 1 | Postgres `trader`, redis db 0 |
| logs | `~/planetaria-logs/live/engine.log` + journal | `~/planetaria-logs/paper/engine.log` + journal |
| strategy plane | never constructed | on |
| LLM | none | Claude CLI on the service user's subscription |
| autodeploy | `planetaria-deploy.timer` (03 min after boot, every 15) | `planetaria-paper-deploy.timer` (10 min after boot, every 15) |

**Why two clones, not one.** `engine.log` rotates inside the working dir
unless `PLANETARIA_LOG_DIR` says otherwise, the SQLite fallback path is
cwd-relative, and two autodeploy timers fetching in one repo would fight.
Separate checkouts make all three impossible by construction. Both units
also set `PLANETARIA_LOG_DIR` outside the checkout — a rotated
`engine.log.1` inside it would read as a dirty tree and stall autodeploy.

**Why Postgres, not the SQLite file.** The fallback was never meant to be
prod; a file inside a checkout that autodeploy rewrites is the wrong home
for state; `pg_dump` backups are trivial; one store type on the box removes
the "which dataset is live" confusion that already existed on Windows (the
Docker Postgres held an older dataset while the engine wrote SQLite). The
rollback is a `DATABASE_URL` swap to the copied SQLite file, kept at
`~/paper-store/trader.db` — outside any checkout on purpose.

## 1. Files

- `deploy/paper/setup-paper.sh` — idempotent: db `trader`, venv, UI build,
  env file, unit, sudoers drop-in. Assumes `deploy/live/setup-linux.sh` ran.
- `deploy/paper/planetaria-paper.service` — the unit (`__USER__/__REPO__/__HOME__` substituted).
- `deploy/paper/paper.env.example` — every var the paper process reads
  (`tests/test_env_examples.py` refuses a key that is not a Settings field).
- `deploy/paper/sudoers-planetaria-paper` — `systemctl */planetaria-paper` + its journal, nothing else.
- `deploy/paper/planetaria-paper-deploy.{service,timer}` — nightly self-deploy;
  runs `deploy/live/autodeploy.sh` of *this* checkout with `PLANETARIA_SERVICE/PORT/MODE` set.
- `backend/app/db/copy_store.py` — the SQLite → Postgres copy tool (below).

## 2. Setup (you, once; needs sudo + keys)

```bash
git clone https://github.com/matthewguoo/planetaria.git ~/planetaria-paper
cd ~/planetaria-paper && bash deploy/paper/setup-paper.sh
sudo nano /etc/planetaria/paper.env        # both PK pairs + FINNHUB_API_KEY
curl -fsSL https://claude.ai/install.sh | bash && claude      # login as mikoyae
claude -p "reply ok" --output-format json  # proves subscription auth from this user
```

The live unit also needs one re-render for the log-dir change (once):

```bash
cd ~/planetaria && sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$HOME/planetaria|g" -e "s|__HOME__|$HOME|g" deploy/live/planetaria-live.service | sudo tee /etc/systemd/system/planetaria-live.service >/dev/null && sudo systemctl daemon-reload && mkdir -p ~/planetaria-logs/live && sudo systemctl restart planetaria-live
```

(outside 09:25–10:00 and 15:45–16:05 ET).

## 3. The copy tool

`python -m app.db.copy_store --source URL --target URL [--dry-run | --verify | --force]`

- opens the **target** with `fallback=False` (a mistyped URL raises; it can never land in `./trader.db`);
- creates the target schema through the app's own fresh-DB path (`create_all` + stamp head — the migration-parity test's guarantee);
- refuses a source not stamped at head, and a non-empty target unless `--force` (wipe first);
- copies every table in FK order (`Base.metadata.sorted_tables`) in batches of 500 with primary keys preserved, naive SQLite datetimes made UTC-aware;
- resets Postgres serial sequences (`plan_events`, `signals`, `strategy_decisions`) to `max(id)` so the journal keeps appending;
- `--verify` compares counts, checks the stamped revision and the sequences. Exit codes: 2 source not at head, 3 target populated, 4 verify failed.

Windows store at the time of writing: trade_plans 17 · plan_events 78 ·
signals ~9.7k · strategy_decisions 65 · strategy_instances 6 · app_settings 2.

## 4. Cutover (outside 20:30–07:30 ET, or a weekend)

1. **Windows, elevated**: `nssm stop planetaria-engine; nssm set planetaria-engine Start SERVICE_DISABLED` — disabled, not just stopped, so a reboot cannot resurrect a second paper enforcer mid-migration.
2. **Snapshot** (one consistent file, no `-wal/-shm` juggling), from the repo root:
   ```bash
   python -c "import sqlite3; sqlite3.connect('backend/trader.db').execute(\"VACUUM INTO 'trader-snapshot.db'\")"
   scp trader-snapshot.db mikoyae@planetaria:paper-store/trader.db
   ```
3. **Box**, from `~/planetaria-paper/backend`:
   ```bash
   S=sqlite+aiosqlite:////home/mikoyae/paper-store/trader.db
   T=postgresql+asyncpg://trader:trader@localhost:5432/trader
   .venv/bin/python -m app.db.copy_store --source $S --target $T --dry-run
   .venv/bin/python -m app.db.copy_store --source $S --target $T
   .venv/bin/python -m app.db.copy_store --source $S --target $T --verify
   ```
4. `sudo systemctl start planetaria-paper`, then:
   - `curl -s 127.0.0.1:8000/api/health` → `"mode":"paper","paper":true,"alpaca_keys_configured":true`
   - `/api/system/state` → `db.engine == "postgres"`, `enforcer.monitors` = open plans, `strategies` populated
   - `/api/system/accounts` → `planetaria1` active; `/api/strategies` → the 6 instances with their states
   - `journalctl -u planetaria-paper` shows `database connected: localhost:5432/trader` — never `POSTGRES UNAVAILABLE`; `stream refused … retrying in 60s` is expected (see §6); no `UniqueViolation` on `plan_events_pkey`.
5. `tailscale serve --bg --http=8000 http://127.0.0.1:8000` (operator; no sudo). Phone: `http://planetaria:8000/terminal` — the API and the websocket are same-origin including the port.
6. `sudo bash deploy/live/install-autodeploy.sh paper` from `~/planetaria-paper`.
7. After one clean session: Windows `nssm remove planetaria-engine confirm`; delete the two `ALPACA_ACCOUNT_LIVE_ROTH_*` lines from the Windows `.env`; `dev.ps1` stays for development. Keep `~/paper-store/trader.db` a week, then delete.

**Rollback**: stop the unit, set `DATABASE_URL=sqlite+aiosqlite:////home/mikoyae/paper-store/trader.db` in `paper.env`, start. Anything written to Postgres after cutover is lost on that path — hence "after one clean session".

## 5. Hygiene addendum

Two enforcers now run on one box, on **two different accounts**; the
one-live-process rule is untouched. Secrets: `paper.env` is root:mikoyae
0640 like `live.env`; neither checkout carries a `.env`. The paper engine's
old LAN binding (`0.0.0.0`, no auth) is retired — the phone reaches paper
the same way it reaches live, over the tailnet. Restart discipline is the
same as live: outside 09:25–10:00 and 15:45–16:05 ET.

## 5b. Its administration window

`http://127.0.0.1:8000/admin` — same panel as the live one (docs/live-server.md
§2c), right half of the box's screen via `deploy/live/install-admin-window.sh`.

## 6. Known interim degradation: the data socket

Alpaca's free plan allows one data websocket per account login. Live holds
it; paper's IEX/option streams are refused (`connection limit exceeded`)
and back off to once a minute (`PatientStockDataStream`). REST polling
covers marks, entry quotes and the enforcer; strategies that consume ticks
run on REST-cadence data until the quote relay lands
(`docs/specs/quote-relay.md`). Watch `feed.stream_age_s` in system state.
