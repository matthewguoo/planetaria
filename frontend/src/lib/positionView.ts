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
import {
  positionValue,
  TRADING_HOURS_PER_YEAR,
  tradingHoursToExpiry,
  type Leg,
  type Smiles,
} from "./optionsMath";
import type { Chain } from "../store/strategyStore";
import { smileFromChain } from "../store/strategyStore";

export type PositionView = {
  legs: (Leg & { symbol: string })[];
  /** Signed net premium basis (actual fill, falling back to plan limit). */
  entryBasis: number;
  /** null = bracketless plan (no TP / no SL to draw). */
  tpPremium: number | null;
  slPremium: number | null;
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
  /** Latest leg expiry (ISO) — tau basis for path reconstruction. */
  expiry: string;
  /** Closed-trade fields: exit event time (trading hours after entry),
   * actual exit premium, reason — drives the EXIT marker + MAE/MFE block. */
  closed: boolean;
  exitMs: number | null;
  exitHours: number | null;
  exitPremium: number | null;
  exitReason: string | null;
  realizedPnl: number | null;
  /** Chunked closing waves (external liquidations land in pieces at
   * different times/prices): one chart marker per wave. Empty = single exit. */
  exitEvents: { hours: number; premium: number; qty: number }[];
};

type OptionPlanLeg = Plan["legs"][number] & { right: "C" | "P"; strike: number; expiry: string };

export function buildPositionView(
  plan: Plan,
  chain: Chain | null,
  mode: "entry" | "live",
): PositionView | null {
  if (!plan.legs.length) return null;
  // Equity plans (share legs: no right/strike/expiry) have no options
  // surface — the chart shows their candles, not a payoff model.
  const optionLegs = plan.legs.filter(
    (l): l is OptionPlanLeg => l.right != null && l.strike != null && l.expiry != null,
  );
  if (optionLegs.length !== plan.legs.length) return null;
  const expiry = optionLegs.reduce((a, l) => (l.expiry > a ? l.expiry : a), optionLegs[0].expiry);
  // Anchor at the actual entry FILL when the journal has it (history rows);
  // created_at (order staging time) is the fallback.
  const anchorMs = Date.parse(plan.entered_at ?? plan.created_at);
  if (!Number.isFinite(anchorMs)) return null;
  const closed = ["closed", "cancelled", "rejected"].includes(plan.status);
  const exitMs = plan.exited_at ? Date.parse(plan.exited_at) : null;

  const legs: (Leg & { symbol: string })[] = optionLegs.map((l) => {
    // live: re-mark IV from the latest chain when the contract is present.
    const contract =
      mode === "live" && chain
        ? chain.contracts.find((c) => c.symbol === l.symbol) ?? null
        : null;
    const iv = contract && contract.iv > 0 ? contract.iv : l.iv ?? 0;
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
  const stopMs = plan.time_stop_utc ? Date.parse(plan.time_stop_utc) : NaN;
  const timeStopHours = Number.isFinite(stopMs)
    ? Math.max(hoursTotal - tradingHoursToExpiry(expiry, stopMs), 0)
    : hoursTotal;

  const exitHours =
    exitMs !== null && Number.isFinite(exitMs)
      ? Math.max(hoursTotal - tradingHoursToExpiry(expiry, exitMs), 0)
      : null;

  return {
    legs,
    entryBasis,
    tpPremium: plan.tp_premium,
    slPremium: plan.sl_premium,
    anchorMs,
    hoursTotal,
    timeStopHours: Math.min(timeStopHours, hoursTotal),
    smiles: mode === "live" ? smileFromChain(chain, expiry) : null,
    strikeSides: optionLegs.map((l) => (l.side >= 0 ? 1 : -1)),
    strikeRights: optionLegs.map((l) => l.right),
    ratios: optionLegs.map((l) => l.ratio || 1),
    strikes: optionLegs.map((l) => l.strike),
    qty: plan.filled_qty || plan.qty,
    label: `${plan.underlying} ${optionLegs
      .map((l) => `${l.side > 0 ? "+" : "−"}${l.ratio || 1}${l.right}${l.strike}`)
      .join(" ")} ×${plan.filled_qty || plan.qty}`,
    expiry,
    closed,
    exitMs: exitMs !== null && Number.isFinite(exitMs) ? exitMs : null,
    exitHours: exitHours !== null ? Math.min(exitHours, hoursTotal) : null,
    exitPremium: plan.exit_premium ?? null,
    exitReason: plan.exit_reason ?? (closed ? plan.status : null),
    realizedPnl: plan.realized_pnl ?? null,
    exitEvents: (plan.exit_fills ?? [])
      .map((f) => {
        const ms = Date.parse(f.ts);
        if (!Number.isFinite(ms)) return null;
        return {
          hours: Math.min(
            Math.max(hoursTotal - tradingHoursToExpiry(expiry, ms), 0),
            hoursTotal,
          ),
          premium: f.premium,
          qty: f.qty,
        };
      })
      .filter((e): e is { hours: number; premium: number; qty: number } => e !== null),
  };
}

/** MAE/MFE over the holding window, reconstructed from 1m underlying bars
 * with the ENTRY-basis legs (frozen fill IVs) — the same pricing the entry
 * projection uses, so the excursion path is consistent with the surface.
 * Each bar is bracketed at its high AND low (the intrabar extreme of a
 * monotone-in-S structure lies at one of them). Dollars per contract-SET. */
export function computeExcursions(
  view: PositionView,
  bars: { t: Float64Array; h: Float64Array; l: Float64Array; n: number },
): { maePerSet: number; mfePerSet: number; maeAtMs: number; mfeAtMs: number; samples: number } | null {
  const endMs = view.exitMs ?? Date.now();
  if (!bars.n || endMs <= view.anchorMs) return null;
  let mae = Infinity;
  let mfe = -Infinity;
  let maeAt = view.anchorMs;
  let mfeAt = view.anchorMs;
  let samples = 0;
  for (let i = 0; i < bars.n; i++) {
    const t = bars.t[i];
    if (t < view.anchorMs || t > endMs) continue;
    const tau = Math.max(tradingHoursToExpiry(view.expiry, t), 0) / TRADING_HOURS_PER_YEAR;
    for (const s of [bars.h[i], bars.l[i]]) {
      const pl = (positionValue(view.legs, s, tau) - view.entryBasis) * 100;
      if (pl < mae) {
        mae = pl;
        maeAt = t;
      }
      if (pl > mfe) {
        mfe = pl;
        mfeAt = t;
      }
    }
    samples++;
  }
  if (!samples || !Number.isFinite(mae) || !Number.isFinite(mfe)) return null;
  return { maePerSet: mae, mfePerSet: mfe, maeAtMs: maeAt, mfeAtMs: mfeAt, samples };
}
