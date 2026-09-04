/**
 * Everything about one position, full-screen on the phone: the head line,
 * the actions, the automation, then every detail. The forms and the grid
 * are the shared components under components/Position/.
 */

import type { OpenOrder, Plan } from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { contractLabel, heldQty, protection } from "../../lib/positionDetail";
import { useHoldingDetail } from "../../lib/useHoldingDetail";
import { useAccountStore } from "../../store/accountStore";
import { PositionCloseAdd, PositionAutomation } from "../Position/PositionActions";
import { PositionDetails } from "../Position/PositionDetails";
import { ProtectionDot } from "./MobileUi";
import { Sheet } from "./Sheet";

export function PositionSheet({
  plan, orders, monitored, onClose, onChart, onAdd,
}: {
  plan: Plan;
  orders: OpenOrder[];
  monitored: boolean | null;
  onClose: () => void;
  onChart: (plan: Plan) => void;
  onAdd: (plan: Plan) => void;
}) {
  const equity = useAccountStore((s) => s.account?.equity);
  const single = plan.legs.length === 1;
  const leg = plan.legs[0];
  const held = heldQty(plan);
  const basis = plan.fill_premium ?? plan.entry_limit;
  const mult = plan.asset_class === "equity" ? 1 : 100;
  const pnl = plan.unrealized_pnl;
  const costBasis = Math.abs(basis) * mult * held;
  const pnlPct = pnl != null && costBasis >= 1 ? (pnl / costBasis) * 100 : null;
  const detail = useHoldingDetail(single ? leg.symbol : null);

  return (
    <Sheet
      title={contractLabel(plan.legs, plan.underlying)}
      onClose={onClose}
      tall
      right={
        <button className="h-9 border border-bb-border px-3 text-[11px] tracking-widest text-bb-muted active:text-bb-amber" onClick={() => onChart(plan)}>
          CHART
        </button>
      }
    >
      <div className="flex items-center gap-3 border-b border-bb-border px-3 py-2">
        <ProtectionDot state={protection(plan)} size={3} />
        <div data-numeric className="flex min-w-0 flex-1 items-baseline gap-3">
          <span className="text-[13px] text-bb-muted">×{held}</span>
          <span className={"text-[20px] font-semibold " + pnlCls(pnl)}>{fmtUsd(pnl, true)}</span>
          <span className={"text-[12px] " + pnlCls(pnl)}>{pnlPct != null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%` : ""}</span>
        </div>
        {monitored === false && <span className="text-[10px] tracking-widest text-bb-loss">NOT MONITORED</span>}
      </div>
      <div className="flex flex-col gap-3 px-3 py-2">
        <PositionCloseAdd plan={plan} onAdd={onAdd} onClosed={onClose} touch />
        <PositionAutomation plan={plan} orders={orders} monitored={monitored} touch />
      </div>
      <PositionDetails plan={plan} detail={detail} equity={equity} touch cols={2} />
    </Sheet>
  );
}
