import type { Designer } from "../../lib/useDesigner";
import {
  STRATEGIES,
  useStrategyStore,
  type StrategyKind,
} from "../../store/strategyStore";
import { PayoffDesigner } from "./PayoffDesigner";

const GROUPS = ["DIRECTIONAL", "SPREADS", "VOLATILITY", "INCOME / NEUTRAL"] as const;

export function StrategyPanel({ designer }: { designer: Designer }) {
  const kind = useStrategyStore((s) => s.kind);
  const setKind = useStrategyStore((s) => s.setKind);
  const expiry = useStrategyStore((s) => s.expiry);
  const setExpiry = useStrategyStore((s) => s.setExpiry);
  const chain = useStrategyStore((s) => s.chain);
  const chainError = useStrategyStore((s) => s.chainError);

  return (
    <div className="panel flex min-w-0 flex-col">
      <div className="panel-title">
        STRATEGY{designer.demo ? " · DEMO CHAIN" : ""}
      </div>
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-1 border-b border-bb-border p-1">
          <select
            className="min-w-0 flex-1 border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
            value={kind}
            onChange={(e) => setKind(e.target.value as StrategyKind)}
            aria-label="Strategy preset"
          >
            {GROUPS.map((group) => (
              <optgroup key={group} label={group}>
                {(Object.keys(STRATEGIES) as StrategyKind[])
                  .filter((k) => STRATEGIES[k].group === group)
                  .map((k) => (
                    <option key={k} value={k}>
                      {STRATEGIES[k].label}
                    </option>
                  ))}
              </optgroup>
            ))}
          </select>
          <select
            className="w-32 border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
            value={expiry ?? ""}
            onChange={(e) => setExpiry(e.target.value)}
            aria-label="Expiration"
          >
            {(chain?.expirations ?? []).map((e) => (
              <option key={e} value={e}>
                {e.slice(5)} ({dteLabel(e)})
              </option>
            ))}
          </select>
        </div>
        {chainError && (
          <div className="truncate px-1 text-[10px] text-bb-loss" title={chainError}>
            CHAIN: {chainError}
          </div>
        )}
        <div className="min-h-0 flex-1 p-1">
          {chain ? (
            <PayoffDesigner designer={designer} />
          ) : (
            <div className="flex h-full items-center justify-center text-[11px] text-bb-muted">
              loading chain…
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function dteLabel(expiry: string): string {
  const now = new Date();
  const et = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(now);
  const today = new Date(`${et}T00:00:00Z`);
  const exp = new Date(`${expiry}T00:00:00Z`);
  const days = Math.round((exp.getTime() - today.getTime()) / 86_400_000);
  return days <= 0 ? "0DTE" : `${days}DTE`;
}
