/**
 * HOME on the phone: the equity curve, then the book — positions, working
 * orders, untracked holdings — as one-line rows. A row opens its sheet;
 * money actions live in the sheets and are two taps. Numbers, dots, no
 * explanations.
 */

import { useState } from "react";
import {
  apiError,
  cancelOpenOrder,
  flattenAll,
  getAccountHistory,
  getOpenOrders,
  getSystemState,
  replaceOpenOrder,
  type OpenOrder,
  type Plan,
  type PortfolioHistory,
  type UntrackedPosition,
} from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { contractLabel, groupOrdersByPlan, heldQty, occLabel, protection } from "../../lib/positionDetail";
import { usePoll } from "../../lib/usePoll";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { EquityCurve } from "../Account/AccountPage";
import { AdoptSheet } from "./AdoptSheet";
import { AccountStrip, Btn, Stepper } from "./MobileUi";
import { PositionRow } from "./PositionRow";
import { PositionSheet } from "./PositionSheet";

const OPEN_STATUS = new Set(["planned", "submitted", "partially_filled", "filled", "exiting"]);
const PERIODS = ["1D", "1W", "1M", "3M", "1A"] as const;
type Period = (typeof PERIODS)[number];

function timeframeFor(period: Period): string {
  return period === "1D" ? "15Min" : period === "1W" ? "1H" : "1D";
}

function etClock(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function useMonitored(): Set<string> | null {
  const [ids, setIds] = useState<Set<string> | null>(null);
  usePoll(async (alive) => {
    try {
      const sys = await getSystemState();
      if (alive()) setIds(new Set(sys.enforcer.monitored_plan_ids));
    } catch {
      if (alive()) setIds(null);
    }
  }, 10_000);
  return ids;
}

function OrderRow({ order, onDone }: { order: OpenOrder; onDone: () => void }) {
  const [mode, setMode] = useState<null | "cancel" | "reprice">(null);
  const [limit, setLimit] = useState(order.limit_price ?? 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setMode(null);
      onDone();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };
  const label = order.legs.length
    ? order.legs.map((l) => `${l.side === "buy" ? "+" : "−"}${l.ratio}× ${l.symbol}`).join(" ")
    : `${order.side.toUpperCase()} ${order.symbol}`;
  return (
    <div className="border-b border-bb-border/60 px-3 py-2">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] text-white">
            {label}
            {order.role && <span className="ml-2 text-[10px] tracking-widest text-bb-muted">{order.role.toUpperCase()}</span>}
          </div>
          <div data-numeric className="mt-0.5 flex gap-3 text-[11px] text-bb-muted">
            <span>{order.type.toUpperCase()}</span>
            <span>{order.filled_qty > 0 ? `${order.filled_qty}/` : ""}{order.qty ?? "—"} @ {order.limit_price != null ? order.limit_price.toFixed(2) : "MKT"}</span>
            <span>{order.status.toUpperCase()}</span>
            <span>{etClock(order.submitted_at)}</span>
          </div>
        </div>
        {mode === "cancel" ? (
          <span className="flex gap-1">
            <Btn kind="danger" disabled={busy} onClick={() => act(() => cancelOpenOrder(order.id))}>CONFIRM</Btn>
            <Btn onClick={() => setMode(null)}>KEEP</Btn>
          </span>
        ) : (
          <span className="flex gap-1">
            {!order.plan_id && order.limit_price != null && (
              <Btn onClick={() => setMode(mode === "reprice" ? null : "reprice")}>REPRICE</Btn>
            )}
            <Btn kind="outline-danger" onClick={() => setMode("cancel")}>CANCEL</Btn>
          </span>
        )}
      </div>
      {mode === "reprice" && (
        <div className="mt-2 flex flex-col gap-2 border border-bb-border p-2">
          <Stepper label="LIMIT" value={limit} set={setLimit} step={Math.max(0.01, Math.abs(limit) * 0.005)} min={0.01} format={(v) => v.toFixed(2)} />
          <Btn kind="primary" disabled={busy || limit === order.limit_price} onClick={() => act(() => replaceOpenOrder(order.id, { limit_price: Number(limit.toFixed(2)) }))}>
            {busy ? "…" : `REPRICE TO ${limit.toFixed(2)}`}
          </Btn>
        </div>
      )}
      {error && <div className="mt-1 text-[11px] text-bb-loss">✗ {error}</div>}
    </div>
  );
}

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="border-t border-bb-border">
      <div className="flex h-9 items-center justify-between px-3">
        <span className="text-[11px] tracking-widest text-bb-amber">{title}</span>
        {right}
      </div>
      {children}
    </div>
  );
}

export function MobileHome({ onChart, onAdd, onAccount }: {
  onChart: (plan: Plan) => void;
  onAdd: (plan: Plan) => void;
  onAccount: () => void;
}) {
  const positions = useAccountStore((s) => s.positions);
  const untracked = useAccountStore((s) => s.untracked);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const { live } = useTradingMode();
  const monitored = useMonitored();
  const [period, setPeriod] = useState<Period>("1M");
  const [history, setHistory] = useState<PortfolioHistory | null>(null);
  const [orders, setOrders] = useState<OpenOrder[]>([]);
  const [sheet, setSheet] = useState<null | { kind: "plan"; id: string } | { kind: "adopt"; symbol: string }>(null);
  const [confirmFlatten, setConfirmFlatten] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  usePoll(async (alive) => {
    try {
      const h = await getAccountHistory(period, timeframeFor(period));
      if (alive()) setHistory(h);
    } catch {
      /* keep the last curve */
    }
  }, 20_000, [period]);

  const loadOrders = async (alive: () => boolean = () => true) => {
    try {
      const o = await getOpenOrders();
      if (alive()) setOrders(o);
    } catch {
      /* keep the last list */
    }
  };
  usePoll(loadOrders, 5_000);

  const open = positions.filter((p) => OPEN_STATUS.has(p.status));
  const unrealized = open.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);
  const { byPlan, loose } = groupOrdersByPlan(orders);
  const sheetPlan = sheet?.kind === "plan" ? positions.find((p) => p.id === sheet.id) ?? null : null;
  const sheetPos: UntrackedPosition | null = sheet?.kind === "adopt" ? untracked.find((u) => u.symbol === sheet.symbol) ?? null : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
      <AccountStrip onClick={onAccount} />

      <div className="flex h-[32dvh] min-h-40 flex-col">
        <div className="flex h-8 shrink-0 items-center gap-1 px-2">
          {PERIODS.map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={"h-7 flex-1 text-[11px] tracking-widest " + (period === p ? "bg-bb-amber font-semibold text-black" : "text-bb-muted")}
            >
              {p}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 px-1 pb-1">
          <EquityCurve history={history} />
        </div>
      </div>

      <Section
        title={`POSITIONS ${open.length}`}
        right={<span data-numeric className={"text-[12px] " + pnlCls(unrealized)}>{fmtUsd(unrealized, true)}</span>}
      >
        {open.map((p) => {
          const basis = p.fill_premium ?? p.entry_limit;
          const held = heldQty(p);
          const cost = Math.abs(basis) * (p.asset_class === "equity" ? 1 : 100) * held;
          const pct = p.unrealized_pnl != null && cost >= 1 ? (p.unrealized_pnl / cost) * 100 : null;
          const isHeld = ["partially_filled", "filled", "exiting"].includes(p.status);
          return (
            <PositionRow
              key={p.id}
              testId={`position-${p.id}`}
              label={contractLabel(p.legs, p.underlying)}
              sub={p.status === "filled" ? undefined : p.status === "partially_filled" ? "PARTIAL" : p.status.toUpperCase()}
              qty={p.status === "partially_filled" && p.filled_qty ? `${p.filled_qty}/${p.qty}` : held}
              basis={basis}
              mark={p.mark ?? null}
              markDim={p.mark_source === "broker"}
              pnl={p.unrealized_pnl}
              pnlPct={pct}
              protection={protection(p)}
              warn={isHeld && monitored !== null && !monitored.has(p.id)}
              onClick={() => setSheet({ kind: "plan", id: p.id })}
            />
          );
        })}
        {!open.length && <div className="px-3 py-4 text-center text-[12px] text-bb-muted">—</div>}
      </Section>

      {(loose.length > 0 || byPlan.size > 0) && (
        <Section title={`ORDERS ${orders.length}`}>
          {orders.map((o) => (
            <OrderRow key={o.id} order={o} onDone={() => { void loadOrders(); void refreshPositions(); }} />
          ))}
        </Section>
      )}

      {untracked.length > 0 && (
        <Section title={<span className="text-bb-orange">UNTRACKED {untracked.length}</span> as unknown as string}>
          {untracked.map((u) => {
            const cost = Math.abs(u.avg_entry_price) * (u.asset_class === "option" ? 100 : 1) * Math.abs(u.qty);
            return (
              <PositionRow
                key={u.symbol}
                testId={`untracked-${u.symbol}`}
                label={occLabel(u)}
                qty={u.qty}
                basis={u.avg_entry_price}
                mark={u.current_price}
                pnl={u.unrealized_pl}
                pnlPct={u.unrealized_pl != null && cost >= 1 ? (u.unrealized_pl / cost) * 100 : null}
                protection="none"
                onClick={() => setSheet({ kind: "adopt", symbol: u.symbol })}
              />
            );
          })}
        </Section>
      )}

      {open.length > 0 && (
        <div className="border-t border-bb-border p-2">
          {confirmFlatten ? (
            <div className="flex gap-1">
              <Btn kind="danger" className="flex-[2]" disabled={busy} onClick={async () => {
                setBusy(true);
                setError(null);
                try {
                  await flattenAll();
                  await refreshPositions();
                } catch (err) {
                  setError(apiError(err));
                } finally {
                  setBusy(false);
                  setConfirmFlatten(false);
                }
              }}>
                {busy ? "…" : `CONFIRM FLATTEN ${open.length}${live ? " · LIVE" : ""}`}
              </Btn>
              <Btn className="flex-1" onClick={() => setConfirmFlatten(false)}>KEEP</Btn>
            </div>
          ) : (
            <Btn kind="outline-danger" className="w-full" onClick={() => setConfirmFlatten(true)}>FLATTEN ALL</Btn>
          )}
          {error && <div className="mt-1 text-[11px] text-bb-loss">✗ {error}</div>}
        </div>
      )}

      {sheetPlan && (
        <PositionSheet
          plan={sheetPlan}
          orders={byPlan.get(sheetPlan.id) ?? []}
          monitored={monitored === null ? null : monitored.has(sheetPlan.id)}
          onClose={() => setSheet(null)}
          onChart={(p) => { setSheet(null); onChart(p); }}
          onAdd={(p) => { setSheet(null); onAdd(p); }}
        />
      )}
      {sheetPos && <AdoptSheet pos={sheetPos} onClose={() => setSheet(null)} />}
    </div>
  );
}
