/**
 * The System blocks, as components rather than as one screen.
 *
 * They were extracted when the console page and the options terminal's drawer
 * both needed them; the drawer is gone, and they stay split because ACCOUNT
 * renders AccountsPanel while SYSTEM renders the other two — choosing the
 * broker account is account management, not a setting.
 *
 * Each panel owns its own fetching. That costs a few duplicate requests when
 * several are mounted together and buys the property that any one of them can
 * be dropped anywhere without a parent having to know what it needs.
 */

import { useCallback, useEffect, useState } from "react";
import {
  apiError,
  getAccounts,
  getFeedSettings,
  getSystemState,
  putFeedSettings,
  selectAccount,
  type AccountList,
  type FeedSettings,
  type SystemState,
} from "../../lib/api";

const POLL_MS = 5_000;

export function Dot({ ok, warn = false }: { ok: boolean; warn?: boolean }) {
  return (
    <span
      className={
        "inline-block h-2 w-2 rounded-full " +
        (ok ? "bg-bb-profit" : warn ? "bg-bb-orange" : "bg-bb-loss")
      }
    />
  );
}

export function Row({
  label,
  ok,
  warn,
  value,
  title,
}: {
  label: string;
  ok: boolean;
  warn?: boolean;
  value: string;
  title?: string;
}) {
  return (
    <div
      className="flex items-center gap-2 border-b border-bb-border/40 px-2 py-1.5"
      title={title}
    >
      <Dot ok={ok} warn={warn} />
      <span className="w-32 shrink-0 text-[10px] tracking-wider text-bb-muted">{label}</span>
      <span data-numeric className="min-w-0 flex-1 truncate text-right text-[11px] text-white">
        {value}
      </span>
    </div>
  );
}

function NumField({
  label, value, onChange, min, max, suffix = "s",
}: {
  label: string; value: number; onChange: (v: number) => void; min: number; max: number; suffix?: string;
}) {
  return (
    <label className="flex items-center justify-between gap-2 px-2 py-1">
      <span className="text-[10px] tracking-wider text-bb-muted">{label}</span>
      <span className="flex items-center gap-1">
        <input
          data-numeric
          type="number"
          min={min}
          max={max}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-16 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-bb-amber outline-none focus:border-bb-amber"
          aria-label={label}
        />
        <span className="text-[10px] text-bb-muted">{suffix}</span>
      </span>
    </label>
  );
}

/** Poll `/api/system/state`. Shared so several panels on one screen do not
 * each run their own timer against the same endpoint. */
export function useSystemState(): SystemState | null {
  const [state, setState] = useState<SystemState | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      getSystemState()
        .then((s) => alive && setState(s))
        .catch(() => {});
    void load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);
  return state;
}

export function HealthPanel({ state }: { state: SystemState | null }) {
  if (!state) return <div className="px-2 py-3 text-[11px] text-bb-muted">loading…</div>;

  const feedSource = !state.feed.configured
    ? state.feed.demo
      ? Object.values(state.feed.sources).includes("public")
        ? "public data (real, keyless)"
        : "synthetic demo"
      : "no keys"
    : `alpaca · age ${state.feed.stream_age_s?.toFixed(0) ?? "—"}s`;

  const noMid = Object.keys(state.enforcer.monitors_without_mid ?? {});

  return (
    <div className="text-[11px]">
      <Row
        label="MARKET DATA"
        ok={state.feed.configured || state.feed.demo}
        warn={!state.feed.configured}
        value={feedSource}
      />
      <Row
        label="SUBSCRIPTIONS"
        ok
        value={`${state.feed.stock_symbols.join(",") || "—"} · ${state.feed.option_symbols} opts`}
      />
      <Row
        label="BROKER"
        ok={state.broker.configured && !state.broker.account_status.startsWith("UNREACHABLE")}
        warn={!state.broker.configured}
        value={`${state.broker.account_status} · PAPER`}
      />
      <Row
        label="TRADING STREAM"
        ok={state.tasks.trading_stream === "running"}
        warn={state.tasks.trading_stream === "not started"}
        value={state.tasks.trading_stream}
      />
      <Row
        label="DATABASE"
        ok={state.db.ok}
        value={`${state.db.engine} · ${state.db.latency_ms ?? "—"}ms`}
      />
      <Row
        label="REDIS"
        ok={state.redis.ok}
        warn={!state.redis.ok}
        value={state.redis.ok ? "connected" : "fallback (in-memory)"}
      />
      <Row
        label="EXIT ENFORCER"
        ok={state.enforcer.ghost_keys === 0 && noMid.length === 0}
        warn
        value={
          `${state.enforcer.monitors} monitors · ${state.enforcer.ghost_keys} ghosts` +
          (noMid.length ? ` · ${noMid.length} NO-QUOTE` : "") +
          (state.enforcer.parked_exits?.length
            ? ` · ${state.enforcer.parked_exits.length} parked`
            : "")
        }
      />
      {noMid.map((pid) => (
        <div
          key={pid}
          className="border-b border-bb-border/40 px-2 py-1 text-[9px] leading-tight text-bb-orange"
          title="This monitor cannot evaluate TP/SL — no quote for the listed legs even via REST polling"
        >
          ⚠ {pid.slice(0, 8)}: {state.enforcer.monitors_without_mid[pid]}
        </div>
      ))}
      <Row
        label="RECONCILE"
        ok={state.tasks.reconcile_loop === "running"}
        warn={state.tasks.reconcile_loop === "not started"}
        value={
          state.enforcer.last_reconcile_age_s != null
            ? `${state.tasks.reconcile_loop} · ${state.enforcer.last_reconcile_age_s.toFixed(0)}s ago`
            : state.tasks.reconcile_loop
        }
      />
      {state.power && (
        <Row
          label="KEEP AWAKE"
          ok={state.power.keep_awake_supported}
          warn={!state.power.keep_awake_supported}
          value={
            !state.power.keep_awake_supported
              ? "unsupported on this OS"
              : state.power.keep_awake_active
                ? "holding — sleep inhibited"
                : "idle (nothing open)"
          }
        />
      )}
    </div>
  );
}

export function AccountsPanel() {
  const [accounts, setAccounts] = useState<AccountList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    getAccounts().then(setAccounts).catch(() => setAccounts(null));
  }, []);

  const pick = async (name: string) => {
    setMsg(null);
    setError(null);
    try {
      setAccounts(await selectAccount(name));
      setMsg("account saved — applies after a backend restart");
    } catch (err) {
      setError(apiError(err));
    }
  };

  if (!accounts) return <div className="px-2 py-1 text-[10px] text-bb-muted">unavailable</div>;

  return (
    <div className="flex flex-col">
      {accounts.accounts.map((a) => (
        <label
          key={a.name}
          className="flex cursor-pointer items-center gap-2 border-b border-bb-border/40 px-2 py-1.5 hover:bg-bb-hover"
        >
          <input
            type="radio"
            name="paper-account"
            checked={a.selected}
            onChange={() => void pick(a.name)}
            aria-label={`Use account ${a.name}`}
          />
          <span className="flex-1 text-[11px] text-bb-amber">{a.name}</span>
          <span className="text-[10px] text-bb-muted" data-numeric>
            {a.key_masked}
          </span>
          {a.active && (
            <span className="border border-bb-profit px-1 text-[9px] text-bb-profit">ACTIVE</span>
          )}
        </label>
      ))}
      {accounts.restart_required && (
        <div className="px-2 py-1 text-[10px] text-bb-orange">
          restart the backend to switch to “{accounts.selected}”
        </div>
      )}
      <div className="px-2 py-1 text-[9px] leading-tight text-bb-muted">
        paper keys only (PK…) from .env: ALPACA_ACCOUNT_&lt;NAME&gt;_API_KEY + _SECRET_KEY.
        Switching is refused while plans are open.
      </div>
      {msg && <div className="px-2 pb-1 text-[10px] text-bb-profit">{msg}</div>}
      {error && <div className="px-2 pb-1 text-[10px] text-bb-loss">{error}</div>}
    </div>
  );
}

export function FeedSettingsPanel() {
  const [cfg, setCfg] = useState<FeedSettings | null>(null);
  const [draft, setDraft] = useState<Partial<FeedSettings>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getFeedSettings().then(setCfg).catch((err) => setError(apiError(err)));
  }, []);

  const merged: FeedSettings | null = cfg ? { ...cfg, ...draft } : null;
  const dirty = Object.keys(draft).length > 0;
  const needsRestart =
    cfg && Object.keys(draft).some((k) => cfg.restart_required_keys.includes(k));

  const apply = useCallback(async () => {
    setMsg(null);
    setError(null);
    try {
      setCfg(await putFeedSettings(draft));
      setDraft({});
      setMsg(needsRestart ? "saved — feed-tier changes apply after a backend restart" : "applied live");
    } catch (err) {
      setError(apiError(err));
    }
  }, [draft, needsRestart]);

  if (!merged) return <div className="px-2 py-3 text-[11px] text-bb-muted">loading…</div>;

  return (
    <div className="flex flex-col">
      <NumField
        label="POSITIONS POLL"
        value={merged.positions_poll_s}
        min={2} max={60}
        onChange={(v) => setDraft((d) => ({ ...d, positions_poll_s: v }))}
      />
      <NumField
        label="ACCOUNT POLL"
        value={merged.account_poll_s}
        min={5} max={300}
        onChange={(v) => setDraft((d) => ({ ...d, account_poll_s: v }))}
      />
      <label className="flex items-center justify-between gap-2 px-2 py-1">
        <span className="text-[10px] tracking-wider text-bb-muted">
          STOCK FEED <span className="text-bb-orange">↻</span>
        </span>
        <select
          value={merged.stock_feed}
          onChange={(e) => setDraft((d) => ({ ...d, stock_feed: e.target.value as "iex" | "sip" }))}
          className="border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
          aria-label="Stock feed"
        >
          <option value="iex">IEX (free)</option>
          <option value="sip">SIP (paid sub)</option>
        </select>
      </label>
      <label className="flex items-center justify-between gap-2 px-2 py-1">
        <span className="text-[10px] tracking-wider text-bb-muted">
          OPTION FEED <span className="text-bb-orange">↻</span>
        </span>
        <select
          value={merged.option_feed}
          onChange={(e) =>
            setDraft((d) => ({ ...d, option_feed: e.target.value as "indicative" | "opra" }))
          }
          className="border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
          aria-label="Option feed"
        >
          <option value="indicative">INDICATIVE (free)</option>
          <option value="opra">OPRA (paid sub)</option>
        </select>
      </label>
      <div className="px-2 py-1 text-[9px] leading-tight text-bb-muted">
        ↻ = applies after backend restart. Poll cadences apply live.
      </div>
      <div className="flex gap-1 px-2 py-2">
        <button className="btn-primary flex-1 text-[11px] disabled:opacity-40" disabled={!dirty} onClick={apply}>
          APPLY
        </button>
        <button
          className="btn-ghost flex-1 text-[11px] disabled:opacity-40"
          disabled={!dirty}
          onClick={() => setDraft({})}
        >
          RESET
        </button>
      </div>
      {msg && <div className="px-2 pb-2 text-[10px] text-bb-profit">{msg}</div>}
      {error && <div className="px-2 pb-2 text-[10px] text-bb-loss">{error}</div>}
    </div>
  );
}
