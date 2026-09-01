import { useEffect, useState } from "react";
import {
  adoptPositions,
  apiError,
  closePosition,
  flattenAll,
  getHistory,
  planStopRisk,
  putRisk,
  tightenExits,
  type Plan,
  type RiskSettings,
  type UntrackedPosition,
} from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { useAccountStore } from "../../store/accountStore";
import { useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import { fmtTimeET } from "../Chart/scales";

type Tab = "POSITIONS" | "HISTORY" | "ACCOUNT";

/** Premium cost basis in dollars for percent-return display. */
function planBasis(plan: Plan): number {
  const premium = Math.abs(plan.fill_premium ?? plan.entry_limit);
  return premium * 100 * (plan.filled_qty || plan.qty);
}

/** "$X (+Y%)" against the premium basis — % omitted when basis is ~0
 * (zero-cost structures have no meaningful percent return). */
function fmtPnl(v: number | null | undefined, basis: number): string {
  if (v === null || v === undefined) return "—";
  const dollars = fmtUsd(v, true);
  if (basis < 1) return dollars;
  return `${dollars} (${v >= 0 ? "+" : ""}${((v / basis) * 100).toFixed(1)}%)`;
}

const etTime = (iso: string) => fmtTimeET(Date.parse(iso));

/** Account % lost if this plan exits at its stop (client-side calc). */
function RiskCell({ plan }: { plan: Plan }) {
  const equity = useAccountStore((s) => s.account?.equity);
  const dollars = planStopRisk(plan);
  return (
    <td
      data-numeric
      className="px-2 py-1 text-right text-bb-orange"
      title={`-$${dollars.toFixed(0)} if stopped out`}
    >
      {equity ? `${((dollars / equity) * 100).toFixed(1)}%` : "—"}
    </td>
  );
}

/** Inline TP editor: percent box relative to the fill basis + premium
 * preview; nothing fires until the explicit APPLY click (two-step). TP is
 * freely movable (either direction); the server still enforces TP > SL. */
function TpCell({ plan, onError }: { plan: Plan; onError: (msg: string | null) => void }) {
  const [editing, setEditing] = useState(false);
  const [pct, setPct] = useState(0);
  const [busy, setBusy] = useState(false);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);

  const basis = plan.fill_premium ?? plan.entry_limit;
  const tp = plan.tp_premium;
  const sl = plan.sl_premium;
  // Bracketless plans (no TP) have nothing to edit here.
  if (tp == null || sl == null) {
    return (
      <td data-numeric className="px-2 py-1 text-right text-bb-muted">
        —
      </td>
    );
  }
  const currentPct = Math.abs(basis) > 0.005 ? ((tp - basis) / Math.abs(basis)) * 100 : 0;
  const preview = basis + (Math.abs(basis) * pct) / 100;
  const valid = preview > sl && Math.abs(preview - tp) > 0.004;

  if (!editing) {
    return (
      <td
        data-numeric
        className="cursor-pointer px-2 py-1 text-right text-bb-profit underline decoration-dotted underline-offset-2"
        title={`TP ${tp.toFixed(2)} (${currentPct >= 0 ? "+" : ""}${currentPct.toFixed(0)}% of basis) — click to edit`}
        onClick={(e) => {
          e.stopPropagation();
          setPct(Math.round(currentPct));
          setEditing(true);
        }}
      >
        {tp.toFixed(2)}
      </td>
    );
  }
  return (
    <td className="px-2 py-1 text-right" onClick={(e) => e.stopPropagation()}>
      <span className="inline-flex items-center gap-1">
        <input
          data-numeric
          type="number"
          step={5}
          autoFocus
          className="w-14 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-bb-profit outline-none focus:border-bb-amber"
          value={pct}
          onChange={(e) => setPct(Number(e.target.value))}
          onKeyDown={(e) => {
            if (e.key === "Escape") setEditing(false);
          }}
          aria-label="Take profit percent of basis"
        />
        <span className="text-[10px] text-bb-muted">%</span>
        <span data-numeric className={"w-12 text-right text-[11px] " + (valid ? "text-bb-profit" : "text-bb-muted")}>
          {preview.toFixed(2)}
        </span>
        <button
          className="border border-bb-profit px-1.5 text-[10px] text-bb-profit hover:bg-bb-profit hover:text-black disabled:opacity-30"
          disabled={busy || !valid}
          title={valid ? `Move TP to ${preview.toFixed(2)}` : "TP must differ and stay above SL"}
          onClick={async () => {
            setBusy(true);
            onError(null);
            try {
              await tightenExits(plan.id, { tp_premium: Number(preview.toFixed(2)) });
              setEditing(false);
              await refreshPositions();
            } catch (err) {
              onError(apiError(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "…" : "APPLY"}
        </button>
        <button
          className="border border-bb-border px-1 text-[10px] text-bb-muted hover:text-bb-amber"
          onClick={() => setEditing(false)}
          aria-label="Cancel TP edit"
        >
          ×
        </button>
      </span>
    </td>
  );
}

function PositionRow({ plan }: { plan: Plan }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const viewPosition = useUiStore((s) => s.viewPosition);
  const setSymbol = useTradingStore((s) => s.setSymbol);

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
    <tr
      className="cursor-pointer border-b border-bb-border/50 hover:bg-bb-hover"
      onClick={() => {
        setSymbol(plan.underlying);
        viewPosition(plan.id);
      }}
      title="View this position on the chart (entry-anchored P/L surface)"
    >
      <td className="px-2 py-1 text-white">
        {plan.underlying}
        <span className="ml-1 text-[10px] text-bb-muted">{legsLabel}</span>
      </td>
      <td data-numeric className="px-2 py-1 text-right">
        {plan.status === "partially_filled" && plan.filled_qty
          ? `${plan.filled_qty}/${plan.qty}`
          : plan.qty}
      </td>
      <td className="px-2 py-1 text-center">
        <span
          className={
            plan.status === "filled"
              ? "text-bb-profit"
              : plan.status === "exiting" || plan.status === "partially_filled"
                ? "text-bb-orange"
                : "text-bb-muted"
          }
        >
          {plan.status === "partially_filled" ? "PARTIAL" : plan.status.toUpperCase()}
        </span>
      </td>
      <td data-numeric className="px-2 py-1 text-right">{(plan.fill_premium ?? plan.entry_limit).toFixed(2)}</td>
      <td data-numeric className="px-2 py-1 text-right">
        {plan.mark != null ? plan.mark.toFixed(2) : <span className="text-bb-orange">STALE</span>}
      </td>
      <td
        data-numeric
        className={"px-2 py-1 text-right " + pnlCls(plan.unrealized_pnl)}
        title="Unrealized P/L ($ and % of premium at risk)"
      >
        {fmtPnl(plan.unrealized_pnl, planBasis(plan))}
      </td>
      <TpCell plan={plan} onError={setError} />
      <td data-numeric className="px-2 py-1 text-right text-bb-loss">
        {plan.sl_premium?.toFixed(2) ?? "—"}
      </td>
      <RiskCell plan={plan} />
      <td data-numeric className="px-2 py-1 text-right text-bb-orange">
        {plan.time_stop_utc ? etTime(plan.time_stop_utc) : "—"}
      </td>
      <td className="px-2 py-1 text-right" onClick={(e) => e.stopPropagation()}>
        <span className="inline-flex gap-1">
          <button
            className="border border-bb-border px-1.5 text-[10px] text-bb-muted hover:text-bb-amber"
            disabled={busy || plan.sl_premium == null}
            title="Tighten SL to halfway between SL and mark"
            onClick={() =>
              act(() => {
                if (plan.sl_premium == null) return Promise.resolve();
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

function UntrackedRow({ pos }: { pos: UntrackedPosition }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);

  const label = pos.occ
    ? `${pos.occ.underlying} ${pos.occ.expiry.slice(5)} ${pos.occ.strike}${pos.occ.right}`
    : pos.symbol;

  return (
    <tr className="border-b border-bb-border/50 bg-bb-orange/5 hover:bg-bb-hover">
      <td className="px-2 py-1 text-white">
        {label}
        <span className="ml-1 text-[9px] text-bb-orange">UNTRACKED</span>
      </td>
      <td data-numeric className="px-2 py-1 text-right">{pos.qty}</td>
      <td className="px-2 py-1 text-center text-[10px] text-bb-orange">LIVE @ BROKER</td>
      <td data-numeric className="px-2 py-1 text-right">{pos.avg_entry_price.toFixed(2)}</td>
      <td data-numeric className="px-2 py-1 text-right">
        {pos.current_price != null ? pos.current_price.toFixed(2) : "—"}
      </td>
      <td data-numeric className={"px-2 py-1 text-right " + pnlCls(pos.unrealized_pl)}>
        {fmtUsd(pos.unrealized_pl, true)}
      </td>
      <td className="px-2 py-1 text-center text-bb-muted" colSpan={4}>
        no exit plan — adopt to enable TP/SL/time-stop enforcement
      </td>
      <td className="px-2 py-1 text-right">
        {pos.occ ? (
          <button
            className="border border-bb-amber px-1.5 text-[10px] text-bb-amber hover:bg-bb-amber hover:text-black"
            disabled={busy}
            title="Create a managed trade plan (server-enforced TP/SL/time stop) for this position"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await adoptPositions([pos.symbol]);
                await refreshPositions();
              } catch (err) {
                setError(apiError(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "…" : "ADOPT"}
          </button>
        ) : (
          <span className="text-[9px] text-bb-muted">STOCK</span>
        )}
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
  const untracked = useAccountStore((s) => s.untracked);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const [busy, setBusy] = useState(false);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 px-2 py-1">
        <span className="text-[10px] text-bb-muted">{positions.length} OPEN</span>
        {untracked.length > 0 && (
          <span className="text-[10px] text-bb-orange">{untracked.length} UNTRACKED @ BROKER</span>
        )}
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
        {positions.length || untracked.length ? (
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
                <th className="px-2 py-1 text-right" title="Account % lost if this position exits at its stop">
                  RISK%
                </th>
                <th className="px-2 py-1 text-right">T-STOP</th>
                <th className="px-2 py-1 text-right">ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <PositionRow key={p.id} plan={p} />
              ))}
              {untracked.map((p) => (
                <UntrackedRow key={p.symbol} pos={p} />
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
  const viewHistorical = useUiStore((s) => s.viewHistorical);
  const setSymbol = useTradingStore((s) => s.setSymbol);
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
                <tr
                  key={t.id}
                  className="cursor-pointer border-b border-bb-border/50 hover:bg-bb-hover"
                  title="Replay this trade on the chart: entry-anchored P/L surface, exit marker, MAE/MFE"
                  onClick={() => {
                    setSymbol(t.underlying);
                    viewHistorical(t);
                  }}
                >
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
                  <td
                    data-numeric
                    className={"px-2 py-1 text-right " + pnlCls(t.realized_pnl)}
                    title="Realized P/L ($ and % of premium at risk)"
                  >
                    {fmtPnl(t.realized_pnl, planBasis(t))}
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
        {numField("max_spread_pct", "MAX LEG SPREAD", 100, "%", 1)}
        {numField("entry_ttl_min", "ENTRY TTL", 1, "m", 1)}
        {numField("max_trades_per_day", "MAX TRADES/DAY", 1, "", 1)}
        {numField("sl_confirm_s", "SL CONFIRM DWELL", 1, "s", 0.5)}
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
