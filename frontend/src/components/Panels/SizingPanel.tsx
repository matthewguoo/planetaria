import type { Designer } from "../../lib/useDesigner";
import { useStrategyStore } from "../../store/strategyStore";

function Row({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="shrink-0 text-[11px] text-bb-muted">{label}</span>
      <span data-numeric className={"truncate text-[12px] " + (cls ?? "text-white")}>
        {value}
      </span>
    </div>
  );
}

export function SizingPanel({ designer }: { designer: Designer }) {
  const qty = useStrategyStore((s) => s.qty);
  const setQty = useStrategyStore((s) => s.setQty);
  const sizing = designer.sizing;

  return (
    <div className="panel flex min-w-0 flex-col">
      <div className="panel-title">SIZING</div>
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-2">
        {sizing ? (
          <>
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-bb-muted">CONTRACTS</span>
              <span className="flex items-center gap-1">
                <input
                  data-numeric
                  className="w-14 border border-bb-border bg-black px-1 py-0.5 text-right text-[12px] text-bb-amber outline-none focus:border-bb-amber"
                  type="number"
                  min={0}
                  max={Math.max(designer.autoQty, 0)}
                  value={qty > 0 ? qty : designer.autoQty}
                  onChange={(e) => setQty(Number(e.target.value))}
                  aria-label="Contracts"
                />
                <button
                  className="border border-bb-border px-1 text-[10px] text-bb-muted hover:text-bb-amber"
                  onClick={() => setQty(0)}
                  title="Auto-size from max loss"
                >
                  AUTO {designer.autoQty}
                </button>
              </span>
            </div>
            <Row
              label={designer.entry < 0 ? "CREDIT RECEIVED" : "COST"}
              value={`$${Math.abs(designer.entry * 100 * designer.qty).toFixed(0)}`}
              cls={designer.entry < 0 ? "text-bb-profit" : undefined}
            />
            <Row
              label="MAX LOSS @ SL"
              value={`-$${(sizing.perContractRisk * designer.qty).toFixed(0)}`}
              cls="text-bb-loss"
            />
            <Row
              label="STRUCTURAL MAX"
              value={
                sizing.contracts > 0
                  ? `-$${((sizing.maxLossStructural / Math.max(sizing.contracts, 1)) * designer.qty).toFixed(0)}`
                  : "—"
              }
              cls="text-bb-loss"
            />
            <Row
              label="BP USED"
              value={`${(designer.equity && sizing.contracts > 0 ? ((sizing.entryCost / sizing.contracts) * designer.qty / designer.equity) * 100 : 0).toFixed(1)}%`}
            />
            <Row
              label="EST. FRICTION"
              value={`-$${(designer.frictionPerSet * designer.qty).toFixed(0)}`}
              cls="text-bb-orange"
            />
            <Row label="EQUITY" value={`$${designer.equity.toLocaleString("en-US", { maximumFractionDigits: 0 })}`} />
            {sizing.reasons.map((reason) => (
              <div key={reason} className="text-[10px] leading-tight text-bb-orange" title={reason}>
                ⚠ {reason}
              </div>
            ))}
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-[11px] text-bb-muted">
            no strategy
          </div>
        )}
      </div>
    </div>
  );
}
