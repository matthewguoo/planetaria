/**
 * Builds the chart-view model for a LIVE POSITION (a TradePlan: legs + exit
 * rules). Two P/L bases:
 *
 * - "entry": the projection as it stood at entry — per-leg IVs frozen from
 *   the fill, no smile, no scenario shocks. "What did I sign up for?"
 * - "live": legs re-marked from the LATEST chain (current IVs per leg,
 *   current smile, scenario shocks apply). "What is it actually worth now?"
 *
 * In both modes P/L is measured against the plan's actual fill premium, and
 * the surface is anchored at ENTRY TIME, spanning entry -> expiry.
 */

import type { Plan } from "./api";
import { tradingHoursToExpiry, type Leg, type Smiles } from "./optionsMath";
import type { Chain } from "../store/strategyStore";
import { smileFromChain } from "../store/strategyStore";

export type PositionView = {
  legs: (Leg & { symbol: string })[];
  /** Signed net premium basis (actual fill, falling back to plan limit). */
  entryBasis: number;
  tpPremium: number;
  slPremium: number;
  /** Entry timestamp (ms) — the surface's time anchor. */
  anchorMs: number;
  /** Trading hours from ENTRY to expiry (total surface span). */
  hoursTotal: number;
  /** Trading hours from ENTRY to the time stop. */
  timeStopHours: number;
  smiles: Smiles | null;
  strikeSides: number[];
  strikeRights: ("C" | "P")[];
  ratios: number[];
  strikes: number[];
  qty: number;
  label: string;
};

export function buildPositionView(
  plan: Plan,
  chain: Chain | null,
  mode: "entry" | "live",
): PositionView | null {
  if (!plan.legs.length) return null;
  const expiry = plan.legs.reduce((a, l) => (l.expiry > a ? l.expiry : a), plan.legs[0].expiry);
  const anchorMs = Date.parse(plan.created_at);
  if (!Number.isFinite(anchorMs)) return null;

  const legs: (Leg & { symbol: string })[] = plan.legs.map((l) => {
    // live: re-mark IV from the latest chain when the contract is present.
    const contract =
      mode === "live" && chain
        ? chain.contracts.find((c) => c.symbol === l.symbol) ?? null
        : null;
    const iv = contract && contract.iv > 0 ? contract.iv : l.iv;
    return {
      symbol: l.symbol,
      right: l.right,
      strike: l.strike,
      qty: l.ratio || 1,
      side: (l.side >= 0 ? 1 : -1) as 1 | -1,
      entry: l.entry,
      iv: iv > 0 ? iv : 0.2, // adopted positions may carry iv=0; degrade loudly-ish
    };
  });

  const entryBasis = plan.fill_premium ?? plan.entry_limit;
  const hoursTotal = tradingHoursToExpiry(expiry, anchorMs);
  const stopMs = Date.parse(plan.time_stop_utc);
  const timeStopHours = Number.isFinite(stopMs)
    ? Math.max(hoursTotal - tradingHoursToExpiry(expiry, stopMs), 0)
    : hoursTotal;

  return {
    legs,
    entryBasis,
    tpPremium: plan.tp_premium,
    slPremium: plan.sl_premium,
    anchorMs,
    hoursTotal,
    timeStopHours: Math.min(timeStopHours, hoursTotal),
    smiles: mode === "live" ? smileFromChain(chain, expiry) : null,
    strikeSides: plan.legs.map((l) => (l.side >= 0 ? 1 : -1)),
    strikeRights: plan.legs.map((l) => l.right),
    ratios: plan.legs.map((l) => l.ratio || 1),
    strikes: plan.legs.map((l) => l.strike),
    qty: plan.filled_qty || plan.qty,
    label: `${plan.underlying} ${plan.legs
      .map((l) => `${l.side > 0 ? "+" : "−"}${l.ratio || 1}${l.right}${l.strike}`)
      .join(" ")} ×${plan.filled_qty || plan.qty}`,
  };
}
