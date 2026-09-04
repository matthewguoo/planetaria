/**
 * Working orders and closed trades as touch rows. Cancelling a working
 * order is a two-tap; a closed trade taps through to its chart replay.
 */

import { useState } from "react";
import { apiError, cancelOpenOrder, getHistory, getOpenOrders, type OpenOrder, type Plan } from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { usePoll } from "../../lib/usePoll";
import { useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";

function etClock(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-US", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

export function MobileOpenOrders() {
  const [orders, setOrders] = useState<OpenOrder[] | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  usePoll(async (alive) => {
    try {
      const next = await getOpenOrders();
      if (alive()) setOrders(next);
    } catch {
      if (alive()) setOrders((o) => o ?? []);
    }
  }, 5_000);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
      {(orders ?? []).map((o) => (
        <div key={o.id} className="border-b border-bb-border/60 px-3 py-2.5">
          <div className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="truncate text-[14px] text-white">
                {o.legs.length
                  ? o.legs.map((l) => `${l.side === "buy" ? "+" : "−"}${l.ratio}× ${l.symbol}`).join(" ")
                  : `${o.side.toUpperCase()} ${o.symbol}`}
              </div>
              <div data-numeric className="mt-0.5 flex gap-3 text-[12px] text-bb-muted">
                <span>{o.type.toUpperCase()}</span>
                <span>
                  {o.filled_qty > 0 ? `${o.filled_qty}/` : ""}
                  {o.qty ?? "—"} @ {o.limit_price != null ? o.limit_price.toFixed(2) : "MKT"}
                </span>
                <span>{o.status.toUpperCase()}</span>
                <span>{etClock(o.submitted_at)} ET</span>
              </div>
            </div>
            {confirm === o.id ? (
              <span className="flex gap-1">
                <button
                  className="h-10 bg-bb-loss px-3 text-[11px] font-semibold text-black"
                  onClick={async () => {
                    try {
                      await cancelOpenOrder(o.id);
                      setOrders(await getOpenOrders());
                    } catch (err) {
                      setError(apiError(err));
                    } finally {
                      setConfirm(null);
                    }
                  }}
                >
                  CONFIRM
                </button>
                <button className="h-10 border border-bb-border px-2 text-[11px] text-bb-muted" onClick={() => setConfirm(null)}>
                  KEEP
                </button>
              </span>
            ) : (
              <button
                className="h-10 border border-bb-loss px-3 text-[11px] text-bb-loss active:bg-bb-loss active:text-black"
                onClick={() => setConfirm(o.id)}
              >
                CANCEL
              </button>
            )}
          </div>
        </div>
      ))}
      {orders && !orders.length && (
        <div className="px-3 py-6 text-center text-[12px] text-bb-muted">—</div>
      )}
      {orders === null && <div className="px-3 py-6 text-center text-[12px] text-bb-muted">…</div>}
      {error && <div className="px-3 py-1 text-[11px] text-bb-loss">✗ {error}</div>}
    </div>
  );
}

export function MobileHistory() {
  const [trades, setTrades] = useState<Plan[] | null>(null);
  const viewHistorical = useUiStore((s) => s.viewHistorical);
  const setSymbol = useTradingStore((s) => s.setSymbol);
  usePoll(async (alive) => {
    try {
      const next = await getHistory();
      if (alive()) setTrades(next);
    } catch {
      if (alive()) setTrades((t) => t ?? []);
    }
  }, 30_000);

  const total = (trades ?? []).reduce((a, t) => a + (t.realized_pnl ?? 0), 0);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b border-bb-border px-3 text-[11px] text-bb-muted">
        <span>{trades?.length ?? 0} CLOSED</span>
        <span data-numeric className={pnlCls(total)}>TOTAL {fmtUsd(total, true)}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {(trades ?? []).map((t) => {
          const basis = Math.abs(t.fill_premium ?? t.entry_limit) * (t.asset_class === "equity" ? 1 : 100) * (t.filled_qty || t.qty);
          const pct = t.realized_pnl != null && basis >= 1 ? (t.realized_pnl / basis) * 100 : null;
          return (
            <button
              key={t.id}
              className="flex w-full items-center gap-3 border-b border-bb-border/60 px-3 py-2.5 text-left"
              onClick={() => { setSymbol(t.underlying); viewHistorical(t); }}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span className="text-[15px] font-semibold text-white">{t.underlying}</span>
                  <span className="truncate text-[12px] text-bb-muted">
                    {t.legs.map((l) => (l.right ? `${l.side > 0 ? "+" : "−"}${l.strike}${l.right}` : `${l.side > 0 ? "LONG" : "SHORT"} SH`)).join(" ")} ×{t.qty}
                  </span>
                </div>
                <div className="mt-0.5 flex gap-3 text-[12px] text-bb-muted">
                  <span>{t.created_at.slice(0, 10)}</span>
                  <span>{(t.exit_reason ?? t.status).toUpperCase()}</span>
                  <span data-numeric>
                    {t.fill_premium?.toFixed(2) ?? "—"} → {t.exit_premium?.toFixed(2) ?? "—"}
                  </span>
                </div>
              </div>
              <div className="shrink-0 text-right">
                <div data-numeric className={"text-[15px] font-semibold " + pnlCls(t.realized_pnl)}>
                  {fmtUsd(t.realized_pnl, true)}
                </div>
                {pct != null && (
                  <div data-numeric className={"text-[11px] " + pnlCls(t.realized_pnl)}>
                    {pct >= 0 ? "+" : ""}{pct.toFixed(1)}%
                  </div>
                )}
              </div>
            </button>
          );
        })}
        {trades && !trades.length && (
          <div className="px-3 py-6 text-center text-[12px] text-bb-muted">—</div>
        )}
      </div>
    </div>
  );
}
