/**
 * One position, one line: label · ×qty · basis→mark · P/L $ · P/L % ·
 * protection dot. Tap opens the sheet. No sentences.
 */

import { fmtUsd, pnlCls } from "../../lib/format";
import type { Protection } from "../../lib/planRisk";
import { ProtectionDot } from "./MobileUi";

export function PositionRow({
  label, sub, qty, basis, mark, markDim = false, pnl, pnlPct, protection, warn, onClick, testId,
}: {
  label: string;
  sub?: string;
  qty: number | string;
  basis: number | null;
  mark: number | null;
  /** Mark came from the broker's position price, not a live quote. */
  markDim?: boolean;
  pnl: number | null | undefined;
  pnlPct: number | null;
  protection: Protection;
  /** Red dot: the enforcer is NOT watching this plan. */
  warn?: boolean;
  onClick: () => void;
  testId?: string;
}) {
  return (
    <button
      className="flex min-h-14 w-full items-center gap-3 border-b border-bb-border/60 px-3 py-2 text-left active:bg-bb-hover"
      onClick={onClick}
      data-testid={testId}
    >
      <ProtectionDot state={protection} size={3} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate text-[15px] font-semibold text-white">{label}</span>
          {sub && <span className="truncate text-[11px] text-bb-muted">{sub}</span>}
          {warn && <span className="inline-block h-2 w-2 rounded-full bg-bb-loss" aria-label="not monitored" />}
        </div>
        <div data-numeric className="mt-0.5 flex gap-3 text-[12px] text-bb-muted">
          <span>×{qty}</span>
          <span>
            {basis != null ? Math.abs(basis).toFixed(2) : "—"} → {mark != null ? Math.abs(mark).toFixed(2) : "—"}
            {markDim && <span className="text-bb-muted"> ·brk</span>}
          </span>
        </div>
      </div>
      <div className="shrink-0 text-right">
        <div data-numeric className={"text-[15px] font-semibold " + pnlCls(pnl)}>{fmtUsd(pnl, true)}</div>
        <div data-numeric className={"text-[11px] " + pnlCls(pnl)}>
          {pnlPct != null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%` : ""}
        </div>
      </div>
    </button>
  );
}
