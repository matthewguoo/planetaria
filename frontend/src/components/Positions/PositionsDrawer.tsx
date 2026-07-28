import { useEffect, useState } from "react";
import {
  apiError,
  closePosition,
  flattenAll,
  getHistory,
  putRisk,
  tightenExits,
  type Plan,
  type RiskSettings,
} from "../../lib/api";
import { useAccountStore } from "../../store/accountStore";

type Tab = "POSITIONS" | "HISTORY" | "ACCOUNT";

const fmtUsd = (v: number | null | undefined, sign = false) =>
  v === null || v === undefined
    ? "—"
    : `${sign && v > 0 ? "+" : ""}${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

const pnlCls = (v: number | null | undefined) =>
  v === null || v === undefined ? "text-bb-muted" : v >= 0 ? "text-bb-profit" : "text-bb-loss";

function etTime(iso: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}

function PositionRow({ plan }: { plan: Plan }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refreshPositions();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  const legsLabel = plan.legs
    .map((l) => `${l.side > 0 ? "+" : "-"}${l.strike}${l.right}`)
    .join(" ");

  return (
    <tr className="border-b border-bb-border/50 hover:bg-bb-hover">
      <td className="px-2 py-1 text-white">
        {plan.underlying}
        <span className="ml-1 text-[10px] text-bb-muted">{legsLabel}</span>
      </td>
      <td data-numeric className="px-2 py-1 text-right">{plan.qty}</td>
      <td className="px-2 py-1 text-center">
        <span
          className={
            plan.status === "filled"
              ? "text-bb-profit"
              : plan.status === "exiting"
                ? "text-bb-orange"
                : "text-bb-muted"
          }
        >
          {plan.status.toUpperCase()}
        </span>
      </td>
      <td data-numeric className="px-2 py-1 text-right">{(plan.fill_premium ?? plan.entry_limit).toFixed(2)}</td>
      <td data-numeric className="px-2 py-1 text-right">
        {plan.mark != null ? plan.mark.toFixed(2) : <span className="text-bb-orange">STALE</span>}
      </td>
      <td data-numeric className={"px-2 py-1 text-right " + pnlCls(plan.unrealized_pnl)}>
        {fmtUsd(plan.unrealized_pnl, true)}
      </td>
      <td data-numeric className="px-2 py-1 text-right text-bb-profit">{plan.tp_premium.toFixed(2)}</td>
      <td data-numeric className="px-2 py-1 text-right text-bb-loss">{plan.sl_premium.toFixed(2)}</td>
      <td data-numeric className="px-2 py-1 text-right text-bb-orange">{etTime(plan.time_stop_utc)}</td>
      <td className="px-2 py-1 text-right">
        <span className="inline-flex gap-1">
          <button
            className="border border-bb-border px-1.5 text-[10px] text-bb-muted hover:text-bb-amber"
            disabled={busy}
            title="Tighten SL to halfway between SL and mark"
            onClick={() =>
              act(() => {
                const mark = plan.mark ?? plan.fill_premium ?? plan.entry_limit;
                return tightenExits(plan.id, {
                  sl_premium: Number(((plan.sl_premium + mark) / 2).toFixed(2)),
                });
              })
            }
          >
            SL▲
          </button>
          <button
            className="border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
            disabled={busy}
            onClick={() => act(() => closePosition(plan.id))}
          >
            CLOSE
          </button>
        </span>
        {error && (
          <div className="max-w-[160px] truncate text-[9px] text-bb-loss" title={error}>
            {error}
          </div>
        )}
      </td>
    </tr>
  );
}

function PositionsTab() {
  const positions = useAccountStore((s) => s.positions);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const [busy, setBusy] = useState(false);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 px-2 py-1">
        <span className="text-[10px] text-bb-muted">{positions.length} OPEN</span>
        <button
          className="ml-auto border border-bb-loss px-2 py-0.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
          disabled={busy || !positions.length}
          onClick={async () => {
            setBusy(true);
            try {
              await flattenAll();
              await refreshPositions();
            } finally {
              setBusy(false);
            }
          }}
        >
          FLATTEN ALL
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {positions.length ? (
          <table className="w-full border-collapse text-[11px]">
            <thead className="sticky top-0 bg-bb-panel text-[10px] text-bb-muted">
              <tr>
                <th className="px-2 py-1 text-left">POSITION</th>
                <th className="px-2 py-1 text-right">QTY</th>
                <th className="px-2 py-1 text-center">STATUS</th>
                <th className="px-2 py-1 text-right">ENTRY</th>
                <th className="px-2 py-1 text-right">MARK</th>
                <th className="px-2 py-1 text-right">UNRLZD</th>
                <th className="px-2 py-1 text-right">TP</th>
                <th className="px-2 py-1 text-right">SL</th>
                <th className="px-2 py-1 text-right">T-STOP</th>
                <th className="px-2 py-1 text-right">ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <PositionRow key={p.id} plan={p} />
              ))}
            </tbody>
          </table>
        ) : (
          <div className="flex h-full items-center justify-center text-[11px] text-bb-muted">
            no open positions
          </div>
        )}
      </div>
    </div>
  );
}

function HistoryTab() {
  const [trades, setTrades] = useState<Plan[] | null>(null);
  useEffect(() => {
    getHistory().then(setTrades).catch(() => setTrades([]));
  }, []);

  const total = (trades ?? []).reduce((acc, t) => acc + (t.realized_pnl ?? 0), 0);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-3 px-2 py-1 text-[10px] text-bb-muted">
        <span>{trades?.length ?? 0} CLOSED</span>
        <span data-numeric className={pnlCls(total)}>
          TOTAL {fmtUsd(total, true)}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {trades?.length ? (
          <table className="w-full border-collapse text-[11px]">
            <thead className="sticky top-0 bg-bb-panel text-[10px] text-bb-muted">
              <tr>
                <th className="px-2 py-1 text-left">DATE</th>
                <th className="px-2 py-1 text-left">POSITION</th>
                <th className="px-2 py-1 text-right">QTY</th>
                <th className="px-2 py-1 text-right">ENTRY</th>
                <th className="px-2 py-1 text-right">EXIT</th>
                <th className="px-2 py-1 text-center">REASON</th>
                <th className="px-2 py-1 text-right">P/L</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.id} className="border-b border-bb-border/50">
                  <td className="px-2 py-1 text-bb-muted">{t.created_at.slice(0, 10)}</td>
                  <td className="px-2 py-1 text-white">
                    {t.underlying}
                    <span className="ml-1 text-[10px] text-bb-muted">
                      {t.legs.map((l) => `${l.side > 0 ? "+" : "-"}${l.strike}${l.right}`).join(" ")}
                    </span>
                  </td>
                  <td data-numeric className="px-2 py-1 text-right">{t.qty}</td>
                  <td data-numeric className="px-2 py-1 text-right">{t.fill_premium?.toFixed(2) ?? "—"}</td>
                  <td data-numeric className="px-2 py-1 text-right">{t.exit_premium?.toFixed(2) ?? "—"}</td>
                  <td className="px-2 py-1 text-center text-[10px] text-bb-muted">
                    {(t.exit_reason ?? t.status).toUpperCase()}
                  </td>
                  <td data-numeric className={"px-2 py-1 text-right " + pnlCls(t.realized_pnl)}>
                    {fmtUsd(t.realized_pnl, true)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="flex h-full items-center justify-center text-[11px] text-bb-muted">
            {trades === null ? "loading…" : "no closed trades yet"}
          </div>
        )}
      </div>
    </div>
  );
}

function AccountTab() {
  const account = useAccountStore((s) => s.account);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const [draft, setDraft] = useState<Partial<RiskSettings>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!account) {
    return <div className="flex flex-1 items-center justify-center text-[11px] text-bb-muted">loading…</div>;
  }
  const risk = { ...account.risk, ...draft };

  const numField = (
    key: keyof RiskSettings,
    label: string,
    scale: number,
    suffix: string,
    step: number,
  ) => (
    <label className="flex items-center justify-between gap-2 text-[11px]">
      <span className="text-bb-muted">{label}</span>
      <span className="flex items-center gap-1">
        <input
          data-numeric
          type="number"
          step={step}
          className="w-16 border border-bb-border bg-black px-1 py-0.5 text-right text-bb-amber outline-none focus:border-bb-amber"
          value={Math.round((risk[key] as number) * scale * 100) / 100}
          onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) / scale })}
        />
        <span className="w-4 text-[10px] text-bb-muted">{suffix}</span>
      </span>
    </label>
  );

  return (
    <div className="flex min-h-0 flex-1 gap-4 overflow-y-auto p-2">
      <div className="flex w-64 shrink-0 flex-col gap-1 text-[11px]">
        <div className="mb-1 text-[10px] tracking-widest text-bb-muted">ACCOUNT ({account.status})</div>
        {(
          [
            ["EQUITY", fmtUsd(account.equity)],
            ["CASH", fmtUsd(account.cash)],
            ["BUYING POWER", fmtUsd(account.buying_power)],
            ["DAY TRADES", String(account.daytrade_count)],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="flex justify-between">
            <span className="text-bb-muted">{label}</span>
            <span data-numeric className="text-white">{value}</span>
          </div>
        ))}
        <div className="flex justify-between">
          <span className="text-bb-muted">DAY REALIZED</span>
          <span data-numeric className={pnlCls(account.day_realized_pnl)}>
            {fmtUsd(account.day_realized_pnl, true)}
          </span>
        </div>
      </div>
      <div className="flex w-72 shrink-0 flex-col gap-1">
        <div className="mb-1 text-[10px] tracking-widest text-bb-muted">RISK RULES (SERVER-ENFORCED)</div>
        {numField("max_loss_pct", "MAX LOSS / TRADE", 100, "%", 0.5)}
        {numField("daily_loss_pct", "DAILY LOSS BREAKER", 100, "%", 0.5)}
        {numField("max_positions", "MAX POSITIONS", 1, "", 1)}
        {numField("bp_cap_pct", "BP CAP", 100, "%", 1)}
        {numField("default_tp_pct", "DEFAULT TP", 100, "%", 5)}
        {numField("default_sl_pct", "DEFAULT SL", 100, "%", 5)}
        <label className="flex items-center justify-between gap-2 text-[11px]">
          <span className="text-bb-muted">TIME STOP (ET)</span>
          <input
            type="time"
            className="border border-bb-border bg-black px-1 py-0.5 text-bb-orange outline-none"
            value={risk.time_stop_et}
            onChange={(e) => setDraft({ ...draft, time_stop_et: e.target.value })}
          />
        </label>
        <label className="flex items-center justify-between gap-2 text-[11px]">
          <span className="text-bb-muted">EXPIRY-DAY STOP (ET)</span>
          <input
            type="time"
            className="border border-bb-border bg-black px-1 py-0.5 text-bb-orange outline-none"
            value={risk.expiry_time_stop_et}
            onChange={(e) => setDraft({ ...draft, expiry_time_stop_et: e.target.value })}
          />
        </label>
        <div className="mt-1 flex items-center gap-2">
          <button
            className="btn-primary px-3 py-1 text-[11px]"
            disabled={saving || !Object.keys(draft).length}
            onClick={async () => {
              setSaving(true);
              setError(null);
              try {
                await putRisk(draft);
                setDraft({});
                await refreshAccount();
              } catch (err) {
                setError(apiError(err));
              } finally {
                setSaving(false);
              }
            }}
          >
            {saving ? "SAVING…" : "SAVE"}
          </button>
          {error && <span className="truncate text-[10px] text-bb-loss" title={error}>{error}</span>}
        </div>
      </div>
    </div>
  );
}

export function PositionsDrawer() {
  const [tab, setTab] = useState<Tab>("POSITIONS");
  const [open, setOpen] = useState(true);
  const positions = useAccountStore((s) => s.positions);

  return (
    <section className={"panel flex shrink-0 flex-col " + (open ? "h-48" : "h-7")}>
      <div className="flex h-7 shrink-0 items-center gap-1 border-b border-bb-border px-1">
        {(["POSITIONS", "HISTORY", "ACCOUNT"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => {
              setTab(t);
              setOpen(true);
            }}
            className={
              "px-2 py-0.5 text-[10px] tracking-widest " +
              (tab === t && open ? "bg-bb-amber font-semibold text-black" : "text-bb-muted hover:text-bb-amber")
            }
          >
            {t}
            {t === "POSITIONS" && positions.length > 0 ? ` (${positions.length})` : ""}
          </button>
        ))}
        <button
          className="ml-auto px-2 text-[10px] text-bb-muted hover:text-bb-amber"
          onClick={() => setOpen(!open)}
        >
          {open ? "▼" : "▲"}
        </button>
      </div>
      {open &&
        (tab === "POSITIONS" ? <PositionsTab /> : tab === "HISTORY" ? <HistoryTab /> : <AccountTab />)}
    </section>
  );
}
