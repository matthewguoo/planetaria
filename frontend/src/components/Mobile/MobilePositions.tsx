/**
 * Positions as cards (the phone-broker pattern): symbol + legs, the P/L
 * big and coloured, the bracket on the second line, a tap to expand into
 * actions. Everything that moves money — CLOSE, FLATTEN, ADOPT — is a
 * two-tap: the first shows exactly what will happen, the second does it.
 * Untracked broker positions (the IRA's ETFs, say) adopt from here with an
 * explicit stop, because a stopless position is the failure mode this
 * whole terminal exists to prevent.
 */

import { useState } from "react";
import {
  adoptPositions,
  apiError,
  closePosition,
  flattenAll,
  getSystemState,
  planStopRisk,
  tightenExits,
  type Plan,
  type UntrackedPosition,
} from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { usePoll } from "../../lib/usePoll";
import { tradingDateAhead } from "../../lib/equityMath";
import { etWallToUtcIso } from "../../lib/et";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import { fmtTimeET } from "../Chart/scales";

const OPEN_STATUS = new Set(["planned", "submitted", "partially_filled", "filled", "exiting"]);

function planBasis(plan: Plan): number {
  const premium = Math.abs(plan.fill_premium ?? plan.entry_limit);
  const mult = plan.asset_class === "equity" ? 1 : 100;
  return premium * mult * (plan.filled_qty || plan.qty);
}

function legsLabel(plan: Plan): string {
  return plan.legs
    .map((l) =>
      l.right != null
        ? `${l.side > 0 ? "+" : "−"}${l.ratio || 1}${l.right}${l.strike}`
        : `${l.side > 0 ? "LONG" : "SHORT"} SH`,
    )
    .join(" ");
}

function countdown(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso) - Date.now();
  if (ms <= 0) return "due";
  const m = Math.floor(ms / 60000);
  if (m >= 36 * 60) return `${Math.round(m / (24 * 60))}d`;
  return m >= 90 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
}

/** Big touch button; `danger` = red fill, `primary` = amber fill. */
function Btn({
  children, onClick, kind = "ghost", disabled, className = "",
}: {
  children: React.ReactNode; onClick: () => void; kind?: "ghost" | "danger" | "primary" | "outline-danger";
  disabled?: boolean; className?: string;
}) {
  const cls = {
    ghost: "border border-bb-border text-bb-muted active:text-bb-amber",
    "outline-danger": "border border-bb-loss text-bb-loss active:bg-bb-loss active:text-black",
    danger: "bg-bb-loss font-semibold text-black active:bg-bb-orange",
    primary: "bg-bb-amber font-semibold text-black active:bg-bb-orange",
  }[kind];
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className={`h-11 px-3 text-[12px] tracking-wider disabled:opacity-40 ${cls} ${className}`}
    >
      {children}
    </button>
  );
}

function useMonitored(): Set<string> | null {
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

function PositionCard({ plan, monitored }: { plan: Plan; monitored: Set<string> | null }) {
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState<null | "close">(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [slDraft, setSlDraft] = useState<number | null>(null);
  const [tpDraft, setTpDraft] = useState<number | null>(null);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const equity = useAccountStore((s) => s.account?.equity);
  const viewPosition = useUiStore((s) => s.viewPosition);
  const setSymbol = useTradingStore((s) => s.setSymbol);
  const { live } = useTradingMode();

  const equityPlan = plan.asset_class === "equity";
  const basis = plan.fill_premium ?? plan.entry_limit;
  const mult = equityPlan ? 1 : 100;
  const qty = plan.filled_qty || plan.qty;
  const pnl = plan.unrealized_pnl;
  const pnlPct = pnl != null && planBasis(plan) >= 1 ? (pnl / planBasis(plan)) * 100 : null;
  const risk = planStopRisk(plan);
  const held = ["partially_filled", "filled", "exiting"].includes(plan.status);
  const watched = monitored === null ? null : monitored.has(plan.id);
  const step = Math.max(0.01, Math.abs(basis) * 0.01);
  const round = (v: number) => Number(v.toFixed(2));

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refreshPositions();
      setConfirm(null);
      setEditing(false);
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  const statusCls =
    plan.status === "filled"
      ? "text-bb-profit"
      : plan.status === "exiting" || plan.status === "partially_filled"
        ? "text-bb-orange"
        : "text-bb-muted";

  return (
    <div className="border-b border-bb-border/60 bg-black">
      <button className="flex w-full items-center gap-3 px-3 py-2.5 text-left" onClick={() => setOpen(!open)}>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="text-[16px] font-semibold text-white">{plan.underlying}</span>
            <span className="truncate text-[12px] text-bb-muted">{legsLabel(plan)}</span>
            <span className={"text-[10px] tracking-wider " + statusCls}>
              {plan.status === "partially_filled" ? "PARTIAL" : plan.status.toUpperCase()}
            </span>
          </div>
          <div data-numeric className="mt-0.5 flex flex-wrap gap-x-3 text-[12px] text-bb-muted">
            <span>×{qty}</span>
            <span title={plan.mark_source === "broker" ? "broker position price (feed dark)" : undefined}>
              {Math.abs(basis).toFixed(2)} → {plan.mark != null ? Math.abs(plan.mark).toFixed(2) : <span className="text-bb-orange">no mark</span>}
              {plan.mark_source === "broker" && <span className="text-bb-muted"> ·brk</span>}
            </span>
            {plan.sl_premium != null && <span className="text-bb-loss">SL {Math.abs(plan.sl_premium).toFixed(2)}</span>}
            {plan.tp_premium != null && <span className="text-bb-profit">TP {Math.abs(plan.tp_premium).toFixed(2)}</span>}
            <span className="text-bb-orange">⏱ {countdown(plan.time_stop_utc)}</span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div data-numeric className={"text-[16px] font-semibold " + pnlCls(pnl)}>
            {fmtUsd(pnl, true)}
          </div>
          <div data-numeric className={"text-[11px] " + pnlCls(pnl)}>
            {pnlPct != null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%` : ""}
          </div>
        </div>
      </button>

      {open && (
        <div className="flex flex-col gap-2 border-t border-bb-border/40 px-3 py-2">
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
            <span className="text-bb-muted">
              RISK @ STOP{" "}
              <span data-numeric className="text-bb-orange">
                -${risk.toFixed(0)}{equity ? ` · ${((risk / equity) * 100).toFixed(1)}%` : ""}
              </span>
            </span>
            {held && (
              <span
                className={
                  watched === null ? "text-bb-muted" : watched ? "text-bb-profit" : "text-bb-loss"
                }
              >
                {watched === null ? "enforcer …" : watched ? "● ENFORCER WATCHING" : "⚠ NOT MONITORED"}
              </span>
            )}
            {plan.time_stop_utc && (
              <span className="text-bb-muted">
                EXIT <span className="text-bb-orange">{fmtTimeET(Date.parse(plan.time_stop_utc))} ET</span>
              </span>
            )}
          </div>

          {editing && plan.sl_premium != null ? (
            <div className="flex flex-col gap-2 border border-bb-border p-2">
              {(
                [
                  ["STOP", slDraft ?? plan.sl_premium, setSlDraft, "text-bb-loss"],
                  ...(plan.tp_premium != null
                    ? ([["TARGET", tpDraft ?? plan.tp_premium, setTpDraft, "text-bb-profit"]] as const)
                    : []),
                ] as const
              ).map(([label, value, set, cls]) => (
                <div key={label} className="flex items-center justify-between">
                  <span className="text-[12px] text-bb-muted">{label}</span>
                  <span className="flex items-center gap-1">
                    <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(round(value - step))}>−</button>
                    <span data-numeric className={"w-20 text-center text-[15px] " + cls}>{Math.abs(value).toFixed(2)}</span>
                    <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(round(value + step))}>+</button>
                  </span>
                </div>
              ))}
              <div className="text-[10px] text-bb-muted">
                loss at new stop ≈ -${(Math.max(basis - (slDraft ?? plan.sl_premium), 0) * mult * qty).toFixed(0)} · server keeps TP above SL
              </div>
              <div className="flex gap-1">
                <Btn kind="primary" className="flex-[2]" disabled={busy}
                  onClick={() => act(() => tightenExits(plan.id, {
                    ...(slDraft != null ? { sl_premium: slDraft } : {}),
                    ...(tpDraft != null ? { tp_premium: tpDraft } : {}),
                  }))}>
                  {busy ? "…" : "APPLY EXITS"}
                </Btn>
                <Btn className="flex-1" onClick={() => { setEditing(false); setSlDraft(null); setTpDraft(null); }}>CANCEL</Btn>
              </div>
            </div>
          ) : confirm === "close" ? (
            <div className="flex flex-col gap-2 border border-bb-loss p-2">
              <div className="text-[12px] text-bb-loss">
                {live && <span className="font-semibold">LIVE · REAL MONEY — </span>}
                close {plan.underlying} ×{qty} at market now? The enforcer's exits are cancelled with it.
              </div>
              <div className="flex gap-1">
                <Btn kind="danger" className="flex-[2]" disabled={busy} onClick={() => act(() => closePosition(plan.id))}>
                  {busy ? "CLOSING…" : "CONFIRM CLOSE"}
                </Btn>
                <Btn className="flex-1" onClick={() => setConfirm(null)}>KEEP</Btn>
              </div>
            </div>
          ) : (
            <div className="flex gap-1">
              <Btn className="flex-1" onClick={() => { setSymbol(plan.underlying); viewPosition(plan.id); }}>
                CHART
              </Btn>
              <Btn className="flex-1" disabled={busy || plan.sl_premium == null}
                onClick={() => act(() => {
                  const mark = plan.mark ?? basis;
                  return tightenExits(plan.id, { sl_premium: round((plan.sl_premium! + mark) / 2) });
                })}
                >
                SL ▲ ½
              </Btn>
              <Btn className="flex-1" disabled={plan.sl_premium == null} onClick={() => setEditing(true)}>
                EDIT
              </Btn>
              <Btn kind="outline-danger" className="flex-1" disabled={busy} onClick={() => setConfirm("close")}>
                CLOSE
              </Btn>
            </div>
          )}
          {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
        </div>
      )}
    </div>
  );
}

function UntrackedCard({ pos }: { pos: UntrackedPosition }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const risk = useAccountStore((s) => s.account?.risk);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const { live } = useTradingMode();
  const stock = pos.asset_class === "stock";
  // Shares: stop as % of the share basis, far backstop. Options: the
  // account's premium defaults (a 50% premium stop is share-nonsense).
  const [slPct, setSlPct] = useState(stock ? 10 : Math.round((risk?.default_sl_pct ?? 0.5) * 100));
  const [tpPct, setTpPct] = useState(0); // 0 = none for shares (run), default for options
  const [days, setDays] = useState(30);

  const label = pos.occ
    ? `${pos.occ.underlying} ${pos.occ.expiry.slice(5)} ${pos.occ.strike}${pos.occ.right}`
    : pos.symbol;
  const basis = pos.avg_entry_price;
  const mult = stock ? 1 : 100;
  const stopPrice = basis * (1 - slPct / 100);
  const lossAtStop = (basis - stopPrice) * mult * Math.floor(Math.abs(pos.qty));

  const adopt = async () => {
    setBusy(true);
    setError(null);
    try {
      await adoptPositions([pos.symbol], {
        sl_pct: slPct / 100,
        ...(stock ? (tpPct > 0 ? { tp_pct: tpPct / 100 } : { tp_pct: 10 }) : {}),
        ...(stock ? { time_stop_utc: etWallToUtcIso(tradingDateAhead(days), "15:55") } : {}),
      });
      await refreshPositions();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  const stepper = (label: string, value: number, set: (v: number) => void, step: number, unit: string, min: number) => (
    <div className="flex items-center justify-between">
      <span className="text-[12px] text-bb-muted">{label}</span>
      <span className="flex items-center gap-1">
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(Math.max(min, value - step))}>−</button>
        <span data-numeric className="w-16 text-center text-[15px] text-white">{value}{unit}</span>
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(value + step)}>+</button>
      </span>
    </div>
  );

  return (
    <div className="border-b border-bb-border/60 bg-bb-orange/5">
      <button className="flex w-full items-center gap-3 px-3 py-2.5 text-left" onClick={() => setOpen(!open)}>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="text-[16px] font-semibold text-white">{label}</span>
            <span className="text-[10px] tracking-wider text-bb-orange">UNTRACKED · NO STOP</span>
          </div>
          <div data-numeric className="mt-0.5 flex gap-3 text-[12px] text-bb-muted">
            <span>×{pos.qty}</span>
            <span>
              {basis.toFixed(2)} → {pos.current_price != null ? pos.current_price.toFixed(2) : "—"}
            </span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div data-numeric className={"text-[16px] font-semibold " + pnlCls(pos.unrealized_pl)}>
            {fmtUsd(pos.unrealized_pl, true)}
          </div>
          <div className="text-[11px] text-bb-orange">{open ? "▴" : "ADOPT ▾"}</div>
        </div>
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t border-bb-border/40 px-3 py-2">
          <div className="text-[11px] text-bb-muted">
            Adopting puts this position under the exit enforcer: a hard stop, an optional target and a time stop, watched server-side.
            {stock && Math.abs(pos.qty) % 1 !== 0 && " Fractional residue stays untracked."}
          </div>
          {stepper("STOP BELOW BASIS", slPct, setSlPct, stock ? 1 : 5, "%", 1)}
          {stock && stepper("TARGET (0 = run)", tpPct, setTpPct, 5, "%", 0)}
          {stock && stepper("TIME STOP", days, setDays, 5, "d", 5)}
          <div data-numeric className="text-[12px] text-bb-muted">
            stop @ <span className="text-bb-loss">{stopPrice.toFixed(2)}</span> · loss at stop{" "}
            <span className="text-bb-loss">-${lossAtStop.toFixed(0)}</span>
          </div>
          <Btn kind={live ? "danger" : "primary"} disabled={busy} onClick={() => void adopt()}>
            {busy ? "ADOPTING…" : `ADOPT WITH ${slPct}% STOP${live ? " (LIVE)" : ""}`}
          </Btn>
          {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
        </div>
      )}
    </div>
  );
}

export function MobilePositions({ compact = false }: { compact?: boolean }) {
  const positions = useAccountStore((s) => s.positions);
  const untracked = useAccountStore((s) => s.untracked);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const monitored = useMonitored();
  const [confirmFlatten, setConfirmFlatten] = useState(false);
  const [busy, setBusy] = useState(false);
  const { live } = useTradingMode();

  const open = positions.filter((p) => OPEN_STATUS.has(p.status));
  const unrealized = open.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b border-bb-border px-3 text-[11px]">
        <span className="text-bb-muted">
          {open.length} OPEN
          {untracked.length ? <span className="text-bb-orange"> · {untracked.length} UNTRACKED</span> : ""}
        </span>
        <span data-numeric className={pnlCls(unrealized)}>
          {fmtUsd(unrealized, true)}
        </span>
        {open.length > 0 && !compact && (
          <button
            className="ml-auto h-8 border border-bb-loss px-2 text-[10px] tracking-wider text-bb-loss active:bg-bb-loss active:text-black"
            onClick={() => setConfirmFlatten(true)}
          >
            FLATTEN ALL
          </button>
        )}
      </div>
      {confirmFlatten && (
        <div className="flex flex-col gap-2 border-b border-bb-loss bg-bb-loss/10 px-3 py-2">
          <div className="text-[12px] text-bb-loss">
            {live && <span className="font-semibold">LIVE · REAL MONEY — </span>}
            close ALL {open.length} positions at market now?
          </div>
          <div className="flex gap-1">
            <Btn kind="danger" className="flex-[2]" disabled={busy}
              onClick={async () => {
                setBusy(true);
                try { await flattenAll(); await refreshPositions(); } finally { setBusy(false); setConfirmFlatten(false); }
              }}>
              {busy ? "FLATTENING…" : "CONFIRM FLATTEN"}
            </Btn>
            <Btn className="flex-1" onClick={() => setConfirmFlatten(false)}>KEEP</Btn>
          </div>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {open.map((p) => (
          <PositionCard key={p.id} plan={p} monitored={monitored} />
        ))}
        {untracked.map((p) => (
          <UntrackedCard key={p.symbol} pos={p} />
        ))}
        {!open.length && !untracked.length && (
          <div className="px-3 py-6 text-center text-[12px] text-bb-muted">no open positions</div>
        )}
      </div>
    </div>
  );
}
