/**
 * Pure helpers behind the phone's position rows and the position sheet:
 * labels, breakeven, expiry countdown, today's move, protection state and
 * the orders-by-plan grouping. No React, no fetching — all tested.
 */

import type { OpenOrder, Plan, PlanLeg, UntrackedPosition } from "./api";
import { planProtection, type Protection } from "./planRisk";

export type { Protection };

/** "SPY 09/18 650C" for one leg, "+1C650 −1C655" for a structure, "AVGG" for shares. */
export function contractLabel(legs: readonly PlanLeg[] | undefined, underlying?: string): string {
  if (!legs || legs.length === 0) return underlying ?? "";
  if (legs.length === 1) {
    const l = legs[0];
    if (l.right == null || l.strike == null) return underlying ?? l.symbol;
    const exp = l.expiry ? ` ${l.expiry.slice(5).replace("-", "/")}` : "";
    return `${underlying ?? l.symbol}${exp} ${l.strike}${l.right}`;
  }
  return legs
    .map((l) => (l.right != null ? `${l.side > 0 ? "+" : "−"}${l.ratio || 1}${l.right}${l.strike}` : `${l.side > 0 ? "LONG" : "SHORT"} SH`))
    .join(" ");
}

export function occLabel(pos: Pick<UntrackedPosition, "symbol" | "occ">): string {
  const o = pos.occ;
  if (!o) return pos.symbol;
  return `${o.underlying} ${o.expiry.slice(5).replace("-", "/")} ${o.strike}${o.right}`;
}

/** Breakeven at expiry for a single-leg long option: strike ± premium. */
export function breakeven(right: "C" | "P", strike: number, premium: number): number {
  return right === "C" ? strike + Math.abs(premium) : strike - Math.abs(premium);
}

export function breakevenPct(be: number, spot: number | null | undefined): number | null {
  if (!spot || spot <= 0) return null;
  return ((be - spot) / spot) * 100;
}

/** "0DTE · 2h10m" / "3d" / "expired". */
export function expiryCountdown(expiryIso: string | null | undefined, nowMs = Date.now()): { dte: number; label: string } {
  if (!expiryIso) return { dte: NaN, label: "—" };
  // Options settle at 16:00 ET on the expiry date; approximate with 20:00 UTC.
  const end = Date.parse(`${expiryIso.slice(0, 10)}T20:00:00Z`);
  const ms = end - nowMs;
  const days = Math.floor(ms / 86_400_000);
  if (ms <= 0) return { dte: 0, label: "expired" };
  if (days === 0) {
    const m = Math.floor(ms / 60_000);
    return { dte: 0, label: `0DTE · ${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m` };
  }
  return { dte: days, label: `${days}d` };
}

/** Time-stop countdown: "due", "45m", "3.5h", "12d". */
export function countdown(iso: string | null | undefined, nowMs = Date.now()): string {
  if (!iso) return "—";
  const ms = Date.parse(iso) - nowMs;
  if (ms <= 0) return "due";
  const m = Math.floor(ms / 60_000);
  if (m >= 36 * 60) return `${Math.round(m / (24 * 60))}d`;
  return m >= 90 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
}

/** Today's move in dollars on the position (shares ×1, contracts ×100). */
export function changeTodayUsd(pos: {
  current_price: number | null;
  lastday_price?: number | null;
  qty: number;
  asset_class: "option" | "stock";
}): number | null {
  if (pos.current_price == null || pos.lastday_price == null) return null;
  const mult = pos.asset_class === "option" ? 100 : 1;
  return (pos.current_price - pos.lastday_price) * pos.qty * mult;
}

export function heldQty(plan: Pick<Plan, "filled_qty" | "qty">): number {
  return plan.filled_qty || plan.qty;
}

/** Protection dot for a managed plan or an untracked row. */
export function protection(row: { sl_premium?: number | null; asset_class?: string; legs?: { side: number }[] } | null): Protection {
  if (!row) return "none";
  return planProtection({
    sl_premium: row.sl_premium ?? null,
    asset_class: row.asset_class,
    legs: row.legs,
    entry_limit: 0,
    qty: 0,
  });
}

export function groupOrdersByPlan(orders: readonly OpenOrder[]): { byPlan: Map<string, OpenOrder[]>; loose: OpenOrder[] } {
  const byPlan = new Map<string, OpenOrder[]>();
  const loose: OpenOrder[] = [];
  for (const o of orders) {
    if (o.plan_id) {
      const list = byPlan.get(o.plan_id) ?? [];
      list.push(o);
      byPlan.set(o.plan_id, list);
    } else {
      loose.push(o);
    }
  }
  return { byPlan, loose };
}

/** Sentence for a close button: "SELL 5 @ MKT" / "BUY 2 @ 1.85 LMT". */
export function closeSentence(side: 1 | -1, qty: number, orderType: "market" | "limit", limit: number | null, all: boolean): string {
  const verb = side > 0 ? "SELL" : "BUY";
  const q = all ? "ALL" : String(qty);
  return orderType === "limit" && limit != null ? `${verb} ${q} @ ${limit.toFixed(2)} LMT` : `${verb} ${q} @ MKT`;
}
