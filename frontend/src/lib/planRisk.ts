/**
 * Per-plan risk math, mirroring backend/app/services/portfolio_risk.py.
 *
 * Two different "at risk" numbers exist on purpose:
 *  - planStopRisk: dollars lost if the plan exits exactly at its stop.
 *    Bracketless plans (sl null) report 0 here — that 0 means "no stop",
 *    not "safe".
 *  - planPremiumAtRisk: for a LONG-ONLY option plan with no stop (the
 *    intrinsic cap), the whole debit paid is the maximum loss.
 * Their sum is the plan's max loss. Equity plans with no stop carry a
 * share notional instead (planUnstoppedNotional), reported but never
 * summed into max loss.
 */

export type RiskPlan = {
  fill_premium?: number | null;
  entry_limit: number;
  sl_premium: number | null;
  filled_qty?: number | null;
  qty: number;
  asset_class?: string;
  legs?: { side: number }[];
};

export type Protection = "stop" | "premium" | "none";

function basisAndQty(plan: RiskPlan): [number, number] {
  const basis = plan.fill_premium ?? plan.entry_limit;
  const qty = plan.filled_qty ?? plan.qty;
  return [basis, Math.max(qty, 0)];
}

export function planStopRisk(plan: RiskPlan): number {
  if (plan.sl_premium == null) return 0;
  const [basis, qty] = basisAndQty(plan);
  const mult = plan.asset_class === "equity" ? 1 : 100;
  return Math.max(basis - plan.sl_premium, 0) * mult * qty;
}

export function planPremiumAtRisk(plan: RiskPlan): number {
  if (plan.sl_premium != null || plan.asset_class === "equity") return 0;
  if ((plan.legs ?? []).some((leg) => leg.side < 0)) return 0;
  const [basis, qty] = basisAndQty(plan);
  return Math.abs(basis) * 100 * qty;
}

export function planUnstoppedNotional(plan: RiskPlan): number {
  if (plan.sl_premium != null || plan.asset_class !== "equity") return 0;
  const [basis, qty] = basisAndQty(plan);
  return Math.abs(basis) * qty;
}

export function planMaxLoss(plan: RiskPlan): number {
  return planStopRisk(plan) + planPremiumAtRisk(plan);
}

export function planProtection(plan: RiskPlan): Protection {
  if (plan.sl_premium != null) return "stop";
  if ((plan.asset_class ?? "option") !== "equity" && !(plan.legs ?? []).some((l) => l.side < 0)) {
    return "premium";
  }
  return "none";
}
