import type { Designer } from "../../lib/useDesigner";
import {
  availableStrikes,
  findContract,
  PLACABILITY_BADGE,
  STRATEGIES,
  strategyPlacability,
  useStrategyStore,
  type StrategyKind,
} from "../../store/strategyStore";

const GROUPS = ["DIRECTIONAL", "SPREADS", "VOLATILITY", "INCOME / NEUTRAL"] as const;

/**
 * Strategy selection + leg editor. The payoff itself lives ON the candle
 * chart (heatmap + right-edge payoff profile); strikes are dragged there on
 * the vertical rail. This panel is the numeric fallback: exact strike
 * selects and ratio steppers per leg.
 */
export function StrategyPanel({ designer }: { designer: Designer }) {
  const kind = useStrategyStore((s) => s.kind);
  const setKind = useStrategyStore((s) => s.setKind);
  const expiry = useStrategyStore((s) => s.expiry);
  const setExpiry = useStrategyStore((s) => s.setExpiry);
  const chain = useStrategyStore((s) => s.chain);
  const chainError = useStrategyStore((s) => s.chainError);
  const strikes = useStrategyStore((s) => s.strikes);
  const ratios = useStrategyStore((s) => s.ratios);
  const rights = useStrategyStore((s) => s.rights);
  const sides = useStrategyStore((s) => s.sides);
  const modified = useStrategyStore((s) => s.modified);
  const edited = useStrategyStore((s) => s.edited);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const setRatio = useStrategyStore((s) => s.setRatio);
  const decRatio = useStrategyStore((s) => s.decRatio);
  const removeLeg = useStrategyStore((s) => s.removeLeg);

  const snaps = availableStrikes(chain, expiry);

  return (
    <div className="panel flex min-w-0 flex-col">
      <div className="panel-title">
        STRATEGY{modified ? " · CUSTOM" : ""}{designer.demo ? " · DEMO CHAIN" : ""}
      </div>
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-1 border-b border-bb-border p-1">
          {/* Once the legs deviate from the preset (strike drags, ratio
              tweaks, chain-panel edits) the select's value moves to a hidden
              sentinel. A controlled <select> fires no change event when the
              picked option equals its current value — so without this,
              re-selecting the displayed preset to reset it did nothing. */}
          <select
            className="min-w-0 flex-1 border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
            value={modified || edited ? "__edited__" : kind}
            onChange={(e) => setKind(e.target.value as StrategyKind)}
            aria-label="Strategy preset"
          >
            {(modified || edited) && (
              <option value="__edited__" disabled hidden>
                {modified ? "CUSTOM" : `${STRATEGIES[kind].label} · EDITED`}
              </option>
            )}
            {GROUPS.map((group) => (
              <optgroup key={group} label={group}>
                {(Object.keys(STRATEGIES) as StrategyKind[])
                  .filter((k) => STRATEGIES[k].group === group)
                  .map((k) => (
                    <option key={k} value={k}>
                      {STRATEGIES[k].label + PLACABILITY_BADGE[strategyPlacability(k)]}
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
        <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-2">
          {strikes.map((_, i) => {
            const contract =
              chain && expiry && strikes[i] !== undefined
                ? findContract(chain, expiry, rights[i], strikes[i])
                : null;
            return (
            <div key={i} className="flex items-center gap-2">
              <span
                className={
                  "w-10 shrink-0 text-[11px] " + (sides[i] > 0 ? "text-bb-amber" : "text-bb-orange")
                }
              >
                {sides[i] > 0 ? "+" : "−"}
                {ratios[i] ?? 1} {rights[i]}
              </span>
              <span
                data-numeric
                className="w-24 shrink-0 truncate text-[9px] text-bb-muted"
                title={
                  contract
                    ? `${contract.bid.toFixed(2)} × ${contract.ask.toFixed(2)} · IV ${(contract.iv * 100).toFixed(1)}%` +
                      (contract.delta !== null ? ` · Δ ${contract.delta.toFixed(3)}` : "")
                    : "no quote"
                }
              >
                {contract
                  ? `${contract.mid.toFixed(2)}${contract.delta !== null ? ` Δ${contract.delta.toFixed(2)}` : ""}`
                  : "—"}
              </span>
              <select
                className="min-w-0 flex-1 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-white outline-none"
                value={strikes[i] ?? ""}
                onChange={(e) => setStrike(i, Number(e.target.value))}
                aria-label={`Leg ${i + 1} strike`}
              >
                {snaps.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
              <span className="flex shrink-0 gap-px">
                <button
                  className="border border-bb-border px-1.5 text-[11px] text-bb-muted hover:text-bb-amber"
                  onClick={() => decRatio(i)}
                  title="Fewer contracts — removes the leg at ×1"
                  aria-label={`Leg ${i + 1} fewer contracts`}
                >
                  −
                </button>
                <button
                  className="border border-bb-border px-1.5 text-[11px] text-bb-muted hover:text-bb-amber"
                  onClick={() => setRatio(i, (ratios[i] ?? 1) + 1)}
                  aria-label={`Leg ${i + 1} more contracts`}
                >
                  +
                </button>
                <button
                  className="border border-bb-border px-1.5 text-[11px] text-bb-muted hover:text-bb-loss disabled:opacity-30"
                  disabled={strikes.length <= 1}
                  onClick={() => removeLeg(i)}
                  aria-label={`Remove leg ${i + 1}`}
                >
                  ×
                </button>
              </span>
            </div>
            );
          })}
          <div className="mt-auto flex items-baseline justify-between text-[11px]">
            <span className="text-bb-muted">
              NET {designer.ready && designer.entry < 0 ? "CREDIT" : "DEBIT"}
            </span>
            <span
              data-numeric
              className={designer.entry < 0 ? "text-bb-profit" : "text-bb-amber"}
            >
              {designer.ready ? Math.abs(designer.entry).toFixed(2) : "—"}
            </span>
          </div>
          <div className="text-[9px] leading-tight text-bb-muted">
            drag strikes on the rail · CHAIN panel adds legs · payoff on-chart
          </div>
          <div
            className="px-1 text-[9px] leading-tight text-bb-muted"
            title="⊘: leaves uncovered short calls — Alpaca rejects these at this account level (verified). $CSP: uncovered short put is cash-secured at ~strike value per set (~$74k on SPY, broker-verified), so it's intrinsically a 1-2 lot structure — add a put wing to size by stop risk instead."
          >
            ⊘ needs naked calls — unplaceable · $CSP cash-secured put — low lot count
          </div>
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
