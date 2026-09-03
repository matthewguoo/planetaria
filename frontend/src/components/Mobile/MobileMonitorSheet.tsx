/**
 * BOT sheet: the phone-sized live view of the strategy runtime for
 * earnings nights out of the house. Self-polling (4s) against the same
 * control-plane APIs the desktop pages use: instance health, tonight's
 * watchlist, the decision journal as it writes, open positions, and a
 * condensed call ticker (data/engine/broker).
 */

import { useState } from "react";
import {
  getMonitorCalls,
  getStrategies,
  getStrategyDecisions,
  type MonitorCall,
  type StrategyDecision,
  type StrategyInstance,
} from "../../lib/api";
import { usePoll } from "../../lib/usePoll";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { decisionColor, decisionSummary } from "../../store/strategyRunnerStore";

const POLL_MS = 4_000;

type WatchRow = { sym: string; day: number | null; run5d: number | null };

function watchlistFrom(decisions: StrategyDecision[]): WatchRow[] {
  for (const d of decisions) {
    const det = d.detail as Record<string, unknown> | null;
    const wl = det?.watchlist as Record<
      string,
      { day_move_pct?: number | null; run5d_pct?: number | null }
    > | undefined;
    if (wl && Object.keys(wl).length) {
      return Object.entries(wl).map(([sym, w]) => ({
        sym,
        day: w.day_move_pct ?? null,
        run5d: w.run5d_pct ?? null,
      }));
    }
  }
  return [];
}

export function MobileMonitorSheet() {
  const positions = useAccountStore((s) => s.positions);
  const [inst, setInst] = useState<StrategyInstance | null>(null);
  const [decisions, setDecisions] = useState<StrategyDecision[]>([]);
  const [calls, setCalls] = useState<MonitorCall[]>([]);
  const [error, setError] = useState(false);

  const { live } = useTradingMode();

  usePoll(async (alive) => {
    try {
      // The live server has no strategy plane (409 on /api/strategies);
      // the sheet degrades to the monitor heartbeat there.
      const instances = live ? [] : await getStrategies();
      // First enabled instance (any kind), else the first at all — the
      // sheet is a heartbeat, not a per-strategy console.
      const earn =
        instances.find((i) => i.state === "enabled") ?? instances[0] ?? null;
      if (!alive()) return;
      setInst(earn);
      if (earn) setDecisions(await getStrategyDecisions(earn.id, 40));
      setCalls((await getMonitorCalls(undefined, 12)).calls);
      if (alive()) setError(false);
    } catch {
      if (alive()) setError(true);
    }
  }, POLL_MS);

  const watch = watchlistFrom(decisions);
  const open = positions.filter((p) => !["closed", "cancelled", "rejected"].includes(p.status));

  const pct = (v: number | null) =>
    v == null ? "" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;

  return (
    <div className="flex flex-col gap-2 p-2 text-[11px]">
      {error && (
        <div className="border border-bb-loss bg-bb-loss/20 px-2 py-1 text-bb-loss">
          backend unreachable — is the engine window still open?
        </div>
      )}

      {/* strategy health */}
      <div className="flex items-center gap-2 border border-bb-border bg-bb-panel px-2 py-1.5">
        <span
          className={
            "h-2 w-2 rounded-full " +
            (inst?.runtime?.task === "running" ? "bg-bb-profit" : "bg-bb-loss")
          }
        />
        <span className="tracking-widest text-bb-amber">
          {inst ? inst.name.toUpperCase() : "NO INSTANCE"}
        </span>
        {inst && (
          <>
            <span
              className={
                "border px-1 text-[9px] " +
                (inst.params.live
                  ? "border-bb-loss text-bb-loss"
                  : "border-bb-muted text-bb-muted")
              }
            >
              {inst.params.live ? "LIVE ORDERS" : "NOTE-MODE"}
            </span>
            <span className="ml-auto text-bb-muted" data-numeric>
              {inst.runtime?.events_seen ?? 0} ev ·{" "}
              {inst.runtime?.errors ? (
                <span className="text-bb-loss">{inst.runtime.errors} err</span>
              ) : (
                "0 err"
              )}
            </span>
          </>
        )}
      </div>

      {/* tonight's watchlist */}
      <div className="border border-bb-border bg-bb-panel">
        <div className="border-b border-bb-border px-2 py-1 text-[9px] tracking-widest text-bb-muted">
          WATCHLIST {watch.length ? `(${watch.length})` : ""}
        </div>
        {watch.length === 0 ? (
          <div className="px-2 py-2 text-bb-muted">freezes at 15:30 ET</div>
        ) : (
          <div className="flex flex-wrap gap-1 p-1.5">
            {watch.map((w) => (
              <span key={w.sym} className="border border-bb-border px-1.5 py-0.5">
                <span className="text-bb-amber">{w.sym}</span>{" "}
                <span
                  data-numeric
                  className={(w.day ?? 0) >= 0 ? "text-bb-profit" : "text-bb-loss"}
                >
                  {pct(w.day)}
                </span>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* open positions */}
      <div className="border border-bb-border bg-bb-panel">
        <div className="border-b border-bb-border px-2 py-1 text-[9px] tracking-widest text-bb-muted">
          OPEN POSITIONS ({open.length})
        </div>
        {open.length === 0 ? (
          <div className="px-2 py-2 text-bb-muted">none</div>
        ) : (
          open.map((p) => (
            <div
              key={p.id}
              className="flex items-center gap-2 border-b border-bb-border/40 px-2 py-1"
            >
              <span className="text-bb-amber">{p.underlying}</span>
              <span className="text-bb-muted">×{p.filled_qty || p.qty}</span>
              <span className="text-bb-muted">{p.status}</span>
              {p.unrealized_pnl != null && (
                <span
                  data-numeric
                  className={
                    "ml-auto " + (p.unrealized_pnl >= 0 ? "text-bb-profit" : "text-bb-loss")
                  }
                >
                  {p.unrealized_pnl >= 0 ? "+" : "−"}${Math.abs(p.unrealized_pnl).toFixed(0)}
                </span>
              )}
            </div>
          ))
        )}
      </div>

      {/* decision journal */}
      <div className="border border-bb-border bg-bb-panel">
        <div className="border-b border-bb-border px-2 py-1 text-[9px] tracking-widest text-bb-muted">
          DECISIONS
        </div>
        {decisions.length === 0 ? (
          <div className="px-2 py-2 text-bb-muted">nothing journaled yet</div>
        ) : (
          <div className="max-h-56 overflow-y-auto">
            {decisions.slice(0, 20).map((d) => (
              <div
                key={d.id}
                className="flex items-start gap-1.5 border-b border-bb-border/40 px-2 py-1"
              >
                <span className="shrink-0 text-bb-muted" data-numeric>
                  {d.ts?.slice(11, 16) ?? "-"}
                </span>
                <span className={"shrink-0 " + decisionColor(d.action)}>{d.action}</span>
                <span className="min-w-0 flex-1 truncate text-bb-muted">
                  {decisionSummary(d)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* call ticker */}
      <div className="border border-bb-border bg-bb-panel">
        <div className="border-b border-bb-border px-2 py-1 text-[9px] tracking-widest text-bb-muted">
          CALLS (data · engine · broker)
        </div>
        {calls.slice(0, 10).map((c, i) => (
          <div
            key={`${c.ts}-${i}`}
            className="flex items-center gap-1.5 border-b border-bb-border/40 px-2 py-0.5"
          >
            <span className="shrink-0 text-[9px] uppercase text-bb-muted">{c.category}</span>
            <span className={"min-w-0 flex-1 truncate " + (c.ok ? "text-bb-neutral" : "text-bb-loss")}>
              {c.name}
            </span>
            {c.ms != null && (
              <span className="shrink-0 text-bb-muted" data-numeric>
                {c.ms}ms
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
