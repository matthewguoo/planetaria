/**
 * The compact book under the chart on the TRADE tab: one line per position,
 * tap to put it on the chart. Everything else about positions - the
 * details, the actions, adoption - lives on HOME and its sheets.
 */

import { contractLabel, heldQty, occLabel, protection } from "../../lib/positionDetail";
import { useAccountStore } from "../../store/accountStore";
import { useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import { PositionRow } from "./PositionRow";

const OPEN_STATUS = new Set(["planned", "submitted", "partially_filled", "filled", "exiting"]);

export function MobilePositions() {
  const positions = useAccountStore((s) => s.positions);
  const untracked = useAccountStore((s) => s.untracked);
  const viewPosition = useUiStore((s) => s.viewPosition);
  const setSymbol = useTradingStore((s) => s.setSymbol);
  const setAssetMode = useTradingStore((s) => s.setAssetMode);
  const open = positions.filter((p) => OPEN_STATUS.has(p.status));

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
      {open.map((p) => {
        const basis = p.fill_premium ?? p.entry_limit;
        const held = heldQty(p);
        const cost = Math.abs(basis) * (p.asset_class === "equity" ? 1 : 100) * held;
        return (
          <PositionRow
            key={p.id}
            label={contractLabel(p.legs, p.underlying)}
            qty={held}
            basis={basis}
            mark={p.mark ?? null}
            markDim={p.mark_source === "broker"}
            pnl={p.unrealized_pnl}
            pnlPct={p.unrealized_pnl != null && cost >= 1 ? (p.unrealized_pnl / cost) * 100 : null}
            protection={protection(p)}
            onClick={() => { setSymbol(p.underlying); setAssetMode(p.asset_class === "equity" ? "equity" : "options"); viewPosition(p.id); }}
          />
        );
      })}
      {untracked.map((u) => (
        <PositionRow
          key={u.symbol}
          label={occLabel(u)}
          qty={u.qty}
          basis={u.avg_entry_price}
          mark={u.current_price}
          pnl={u.unrealized_pl}
          pnlPct={null}
          protection="none"
          onClick={() => { setSymbol(u.occ ? u.occ.underlying : u.symbol); setAssetMode(u.occ ? "options" : "equity"); }}
        />
      ))}
      {!open.length && !untracked.length && <div className="px-3 py-4 text-center text-[12px] text-bb-muted">—</div>}
    </div>
  );
}
