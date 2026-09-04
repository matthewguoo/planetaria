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

import type { Plan, UntrackedPosition } from "./api";
import {
  positionValue,
  TRADING_HOURS_PER_YEAR,
  tradingHoursToExpiry,
  type Leg,
  type Smiles,
} from "./optionsMath";
import { tradingDateAhead } from "./equityMath";
import { etWallToUtcIso } from "./et";
import { contractLabel, heldQty, occLabel } from "./positionDetail";
import { tradingHoursBetween } from "./tradingTime";
import type { Chain } from "../store/strategyStore";
import { smileFromChain } from "../store/strategyStore";
import type { ExitDraft } from "../store/exitDraftStore";

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

/** The exits a plan draws: its own rules, or the draft being edited. */
export function planExits(plan: Plan, draft: ExitDraft | null): ExitDraft {
  return draft ?? { sl: plan.sl_premium, tp: plan.tp_premium, timeStopUtc: plan.time_stop_utc };
}

export function buildPositionView(
  plan: Plan,
  chain: Chain | null,
  mode: "entry" | "live",
  draft: ExitDraft | null = null,
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
  const exits = planExits(plan, draft);
  const hoursTotal = tradingHoursToExpiry(expiry, anchorMs);
  const stopMs = exits.timeStopUtc ? Date.parse(exits.timeStopUtc) : NaN;
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
    tpPremium: exits.tp,
    slPremium: exits.sl,
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

/** The exit draft an untracked row starts from: shares get a 10% stop and
 * a 30-trading-day exit; options the account's default stop distance (0 =
 * no stop, the premium is the stop). Signed like the plan fields. */
export function adoptSeed(pos: UntrackedPosition, defaultSlPct: number, now: Date = new Date()): ExitDraft {
  const side: 1 | -1 = pos.qty >= 0 ? 1 : -1;
  const basis = Math.abs(pos.avg_entry_price);
  if (pos.asset_class === "stock") {
    return {
      sl: Number((side * basis * (1 - side * 0.1)).toFixed(4)),
      tp: null,
      timeStopUtc: etWallToUtcIso(tradingDateAhead(30, now), "15:55"),
    };
  }
  const pct = Math.min(Math.max(defaultSlPct, 0), 0.95);
  return { sl: pct > 0 ? Number((side * basis * (1 - side * pct)).toFixed(4)) : null, tp: null, timeStopUtc: null };
}

/** The chart-view model for an UNTRACKED single-leg option: the broker row
 * is the whole story (no plan, no fill IV), so the leg is marked at the
 * given IV (the contract's live IV; a flat 20% when unknown), P/L is
 * measured against the broker's average entry, and the surface anchors at
 * the entry time the broker's fills gave (now, when unknown). The exits
 * come from the adopt draft — null stop = the premium is the stop. */
export function buildUntrackedView(
  pos: UntrackedPosition,
  opts: { iv: number | null; enteredAt: string | null; exits: ExitDraft; nowMs?: number },
): PositionView | null {
  const occ = pos.occ;
  if (!occ || pos.qty === 0) return null;
  const side: 1 | -1 = pos.qty > 0 ? 1 : -1;
  const iv = opts.iv && opts.iv > 0 ? opts.iv : 0.2;
  const entry = Math.abs(pos.avg_entry_price);
  const legs: (Leg & { symbol: string })[] = [
    { symbol: pos.symbol, right: occ.right, strike: occ.strike, qty: 1, side, entry, iv },
  ];
  const now = opts.nowMs ?? Date.now();
  const entered = opts.enteredAt ? Date.parse(opts.enteredAt) : NaN;
  const anchorMs = Number.isFinite(entered) && entered < now ? entered : now;
  const hoursTotal = tradingHoursToExpiry(occ.expiry, anchorMs);
  const stopMs = opts.exits.timeStopUtc ? Date.parse(opts.exits.timeStopUtc) : NaN;
  const timeStopHours = Number.isFinite(stopMs)
    ? Math.min(Math.max(hoursTotal - tradingHoursToExpiry(occ.expiry, stopMs), 0), hoursTotal)
    : hoursTotal;
  return {
    legs,
    entryBasis: side * entry,
    tpPremium: opts.exits.tp,
    slPremium: opts.exits.sl,
    anchorMs,
    hoursTotal,
    timeStopHours,
    smiles: null,
    strikeSides: [side],
    strikeRights: [occ.right],
    ratios: [1],
    strikes: [occ.strike],
    qty: Math.floor(Math.abs(pos.qty)),
    label: `${occLabel(pos)} ×${Math.floor(Math.abs(pos.qty))}`,
    expiry: occ.expiry,
    closed: false,
    exitMs: null,
    exitHours: null,
    exitPremium: null,
    exitReason: null,
    realizedPnl: null,
    exitEvents: [],
  };
}

/** The chart-view model for a SHARE position (a managed equity plan or an
 * untracked stock row): entry price and time, the stop / target as prices,
 * the exit day. `editable` = the lines drag (open plan or untracked). */
export type EquityPosition = {
  key: string;
  label: string;
  side: 1 | -1;
  shares: number;
  entryPx: number;
  /** Absolute prices; null = none. */
  sl: number | null;
  tp: number | null;
  anchorMs: number;
  timeStopMs: number | null;
  /** Trading hours from entry to the exit day (null = no time stop). */
  timeStopHours: number | null;
  editable: boolean;
};

function shareExits(exits: ExitDraft, side: 1 | -1) {
  // Plan convention: signed on the value axis (side * price).
  const abs = (v: number | null) => (v == null ? null : Math.abs(v));
  void side;
  return { sl: abs(exits.sl), tp: abs(exits.tp) };
}

export function equityPositionOfPlan(plan: Plan, draft: ExitDraft | null, nowMs = Date.now()): EquityPosition | null {
  if (plan.asset_class !== "equity" || !plan.legs.length) return null;
  const leg = plan.legs[0];
  const side: 1 | -1 = leg.side >= 0 ? 1 : -1;
  const anchorMs = Date.parse(plan.entered_at ?? plan.created_at);
  if (!Number.isFinite(anchorMs)) return null;
  const exits = planExits(plan, draft);
  const closed = ["closed", "cancelled", "rejected"].includes(plan.status);
  const stopMs = exits.timeStopUtc ? Date.parse(exits.timeStopUtc) : NaN;
  return {
    key: plan.id,
    label: contractLabel(plan.legs, plan.underlying),
    side,
    shares: heldQty(plan),
    entryPx: Math.abs(plan.fill_premium ?? plan.entry_limit),
    ...shareExits(exits, side),
    anchorMs,
    timeStopMs: Number.isFinite(stopMs) ? stopMs : null,
    timeStopHours: Number.isFinite(stopMs) ? tradingHoursBetween(anchorMs, stopMs) : null,
    editable: !closed && ["partially_filled", "filled"].includes(plan.status) && nowMs > 0,
  };
}

export function equityPositionOfUntracked(
  pos: UntrackedPosition,
  opts: { enteredAt: string | null; exits: ExitDraft; nowMs?: number },
): EquityPosition | null {
  if (pos.occ || pos.asset_class !== "stock" || pos.qty === 0) return null;
  const side: 1 | -1 = pos.qty > 0 ? 1 : -1;
  const now = opts.nowMs ?? Date.now();
  const entered = opts.enteredAt ? Date.parse(opts.enteredAt) : NaN;
  const anchorMs = Number.isFinite(entered) && entered < now ? entered : now;
  const stopMs = opts.exits.timeStopUtc ? Date.parse(opts.exits.timeStopUtc) : NaN;
  return {
    key: pos.symbol,
    label: pos.symbol,
    side,
    shares: Math.floor(Math.abs(pos.qty)),
    entryPx: Math.abs(pos.avg_entry_price),
    ...shareExits(opts.exits, side),
    anchorMs,
    timeStopMs: Number.isFinite(stopMs) ? stopMs : null,
    timeStopHours: Number.isFinite(stopMs) ? tradingHoursBetween(anchorMs, stopMs) : null,
    editable: true,
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
