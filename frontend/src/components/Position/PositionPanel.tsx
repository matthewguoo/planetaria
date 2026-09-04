/**
 * Desktop position view: when the chart is inspecting a position, the
 * ordering panels under it give way to this — DETAILS (every field a
 * brokerage page shows), ACTIONS (close all/N at MKT/LMT, add) and
 * AUTOMATION (give or move the exits, the orders the engine holds). An
 * untracked broker position shows its details and the adopt form instead.
 * ✕ returns the ticket panels.
 */

import { useState } from "react";
import { getHoldingDetail, getOpenOrders, type HoldingDetail, type OpenOrder, type Plan } from "../../lib/api";
import { contractLabel, groupOrdersByPlan, occLabel, protection } from "../../lib/positionDetail";
import { useMonitored } from "../../lib/useMonitored";
import { usePoll } from "../../lib/usePoll";
import { useAccountStore } from "../../store/accountStore";
import { useUiStore } from "../../store/uiStore";
import { ProtectionDot } from "../Mobile/MobileUi";
import { AdoptForm } from "./AdoptForm";
import { PositionCloseAdd, PositionAutomation } from "./PositionActions";
import { PositionDetails } from "./PositionDetails";

function useDetail(symbol: string | null): HoldingDetail | null {
  const [detail, setDetail] = useState<HoldingDetail | null>(null);
  usePoll(async (alive) => {
    if (!symbol) return;
    try {
      const d = await getHoldingDetail(symbol);
      if (alive()) setDetail(d);
    } catch {
      /* the panel still shows what the plan knows */
    }
  }, 5_000, [symbol]);
  return symbol ? detail : null;
}

function useOrders(): OpenOrder[] {
  const [orders, setOrders] = useState<OpenOrder[]>([]);
  usePoll(async (alive) => {
    try {
      const o = await getOpenOrders();
      if (alive()) setOrders(o);
    } catch {
      /* keep the last list */
    }
  }, 5_000);
  return orders;
}

function Panel({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="panel flex min-h-0 flex-col">
      <div className="panel-title flex items-center justify-between">
        <span>{title}</span>
        {right}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}

export function PositionPanel({ onAdd }: { onAdd: (plan: Plan) => void }) {
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const viewingUntracked = useUiStore((s) => s.viewingUntracked);
  const viewedHistorical = useUiStore((s) => s.viewedHistorical);
  const closePositionView = useUiStore((s) => s.closePositionView);
  const positions = useAccountStore((s) => s.positions);
  const untracked = useAccountStore((s) => s.untracked);
  const equity = useAccountStore((s) => s.account?.equity);
  const monitored = useMonitored();
  const orders = useOrders();

  const plan = viewingPlanId
    ? positions.find((p) => p.id === viewingPlanId) ?? (viewedHistorical?.id === viewingPlanId ? viewedHistorical : null)
    : null;
  const pos = !plan && viewingUntracked ? untracked.find((u) => u.symbol === viewingUntracked) ?? null : null;
  const detailSymbol = plan ? (plan.legs.length === 1 ? plan.legs[0].symbol : null) : pos?.symbol ?? null;
  const detail = useDetail(detailSymbol);
  if (!plan && !pos) return null;

  const closed = plan ? ["closed", "cancelled", "rejected"].includes(plan.status) : false;
  const label = plan ? contractLabel(plan.legs, plan.underlying) : occLabel(pos!);
  const back = (
    <button className="px-2 text-[11px] text-bb-muted hover:text-bb-amber" onClick={closePositionView} title="Back to the ticket">
      ✕ TICKET
    </button>
  );

  return (
    <section className="grid max-h-[44vh] shrink-0 auto-rows-[19rem] grid-cols-1 gap-px overflow-y-auto md:grid-cols-3">
      <Panel
        title={label}
        right={
          <span className="flex items-center gap-2">
            <ProtectionDot state={plan ? protection(plan) : "none"} />
            {back}
          </span>
        }
      >
        <PositionDetails plan={plan} pos={pos} detail={detail} equity={equity} cols={2} />
      </Panel>
      {plan ? (
        <>
          <Panel title={closed ? "CLOSED" : "ACTIONS"}>
            <div className="p-2">
              {closed ? (
                <div className="text-[11px] text-bb-muted">
                  {(plan.exit_reason ?? plan.status).toUpperCase()} · realized {plan.realized_pnl != null ? `${plan.realized_pnl >= 0 ? "+" : "−"}$${Math.abs(plan.realized_pnl).toFixed(0)}` : "—"}
                </div>
              ) : (
                <PositionCloseAdd plan={plan} onAdd={onAdd} onClosed={closePositionView} />
              )}
            </div>
          </Panel>
          <Panel title="AUTOMATION">
            <div className="p-2">
              <PositionAutomation plan={plan} orders={groupOrdersByPlan(orders).byPlan.get(plan.id) ?? []} monitored={monitored === null ? null : monitored.has(plan.id)} />
            </div>
          </Panel>
        </>
      ) : (
        <Panel title="ADOPT · PUT IT UNDER THE ENFORCER">
          <div className="p-2">
            <AdoptForm pos={pos!} />
          </div>
        </Panel>
      )}
    </section>
  );
}
