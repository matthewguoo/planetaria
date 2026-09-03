/**
 * Signed-space arithmetic for the manual EQUITY swing ticket. The engine's
 * position-value convention holds for shares exactly as for options:
 * entry = side * price, and SL < entry (< TP when a TP exists) on that axis
 * for longs AND shorts. The formulas are ref_tick's (the deleted equity
 * integration-proof strategy) verbatim — the backend prices exits on the
 * same convention, so these must not drift.
 */


const round2 = (v: number) => Math.round(v * 100) / 100;

export type EquityExits = {
  /** Signed entry on the position-value axis (side * price). */
  entry: number;
  /** Hard stop, below entry on the value axis. */
  sl: number;
  /** Optional target, above entry; null = let the winner run. */
  tp: number | null;
};

/** slPct / tpPct are positive fractions of price (0.05 = 5%). */
export function equityExits(
  price: number,
  side: 1 | -1,
  slPct: number,
  tpPct: number | null,
): EquityExits {
  const entry = side * price;
  const sl = round2(side * price * (1 - side * slPct));
  const tp = tpPct != null ? round2(side * price * (1 + side * tpPct)) : null;
  return { entry, sl, tp };
}

/** Whole shares whose at-stop loss fits the risk budget. Signed space keeps
 * entry - sl > 0 for both directions. */
export function sharesForRisk(riskBudgetUsd: number, entry: number, sl: number): number {
  const perShare = entry - sl;
  if (perShare <= 0 || riskBudgetUsd <= 0) return 0;
  return Math.floor(riskBudgetUsd / perShare);
}

/** Reward/risk multiple; null when no target. */
export function rr(entry: number, tp: number | null, sl: number): number | null {
  const risk = entry - sl;
  if (tp == null || risk <= 0) return null;
  return (tp - entry) / risk;
}

/** Capital the entry consumes against the book (short = Reg-T 150%). */
export function capitalUsd(price: number, shares: number, side: 1 | -1): number {
  return Math.abs(price) * shares * (side < 0 ? 1.5 : 1.0);
}

export type EquityPreflightArgs = {
  price: number;
  side: 1 | -1;
  shares: number;
  exits: EquityExits;
  /** The account's per-trade max-loss cap in dollars (risk.max_loss_pct ×
   * equity); null = unknown, skip the check (server still enforces). */
  maxLossCapUsd: number | null;
  equityLongOnly: boolean;
  quoteFresh: boolean;
};

/** Mirrors the backend gates so refusals show BEFORE submit. Advisory only —
 * the server re-validates everything. */
export function equityPreflight(a: EquityPreflightArgs): string[] {
  const reasons: string[] = [];
  if (!a.quoteFresh || a.price <= 0) reasons.push("no fresh quote — cannot price the entry");
  if (a.side < 0 && a.equityLongOnly)
    reasons.push("shorts disabled (equity_long_only) — flip it in risk settings first");
  if (a.exits.sl >= a.exits.entry) reasons.push("stop must sit below entry (tighter than 0%)");
  if (a.exits.tp != null && a.exits.tp <= a.exits.entry)
    reasons.push("target must sit above entry");
  if (a.shares < 1) reasons.push("risk budget sizes to 0 shares — widen risk % or tighten stop");

  if (a.maxLossCapUsd != null) {
    const maxLoss = (a.exits.entry - a.exits.sl) * a.shares;
    if (maxLoss > a.maxLossCapUsd + 0.01)
      reasons.push(
        `max loss $${maxLoss.toFixed(0)} exceeds the account's per-trade cap ` +
          `$${a.maxLossCapUsd.toFixed(0)}`,
      );
  }
  return reasons;
}

/** Default far time-stop backstop for open-ended swings: +30 days, so the
 * enforcer always has a hard exit even if the trader forgets the trade. */
export function swingBackstopUtc(now: Date = new Date()): string {
  return new Date(now.getTime() + 30 * 24 * 3600 * 1000).toISOString();
}

// ------------------------------------------------------- smart stop levels
// The edge of running our own stack: the stop suggestion comes from the
// symbol's OWN measured volatility, not a habit number. House research
// backs the shape (wick study): winners routinely trade ~1σ against the
// entry before paying, so a stop inside the hold-horizon noise band is a
// shakeout machine, not protection.

/** Daily 1σ move (% of price) from annualized realized vol (fraction). */
export function dailySigmaPct(rvAnnualized: number): number {
  return rvAnnualized > 0 ? (rvAnnualized / Math.sqrt(252)) * 100 : 0;
}

/** 1σ expected |move| (% of price) over a hold of N trading days. */
export function holdSigmaPct(dailySigma: number, holdDays: number): number {
  return dailySigma * Math.sqrt(Math.max(holdDays, 0.5));
}

/** Suggested stop distance: k× the hold-horizon 1σ (default 1.5×), so
 * ordinary noise doesn't tag it; clamped to [1%, 30%], rounded to 0.5. */
export function suggestedStopPct(
  dailySigma: number, holdDays: number, k = 1.5,
): number | null {
  if (dailySigma <= 0) return null;
  const raw = k * holdSigmaPct(dailySigma, holdDays);
  return Math.min(30, Math.max(1, Math.round(raw * 2) / 2));
}

/** Trading days until an ISO date (yyyy-mm-dd), floor 1; crude 5/7 scale. */
export function holdDaysUntil(dateStr: string, now: Date = new Date()): number {
  const target = Date.parse(`${dateStr}T20:00:00Z`);
  if (!Number.isFinite(target)) return 1;
  const cal = (target - now.getTime()) / 86_400_000;
  return Math.max(1, Math.round(cal * (5 / 7)));
}

// ------------------------------------------------------ automatic horizon
// The stop and the hold horizon are one decision seen from two sides: a
// stop k× the horizon's 1σ (suggestedStopPct) inverts to "how long does a
// stop of this width buy me before ordinary noise reaches it". That is the
// automatic time stop — the planned exit is where the stop stops being a
// disaster line and starts being a coin flip against the symbol's own vol.

/** Trading days a stop of `slPct` covers at k× the horizon 1σ; [1, 30]. */
export function autoHoldDays(slPct: number, dailySigma: number, k = 1.5): number | null {
  if (dailySigma <= 0 || slPct <= 0) return null;
  const days = (slPct / (k * dailySigma)) ** 2;
  return Math.min(30, Math.max(1, Math.round(days)));
}

/** ET calendar date (yyyy-mm-dd) `tradingDays` weekdays ahead of `now`.
 * Weekends skipped; exchange holidays are not (the enforcer's clock parks
 * a holiday exit to the next session anyway). */
export function tradingDateAhead(tradingDays: number, now: Date = new Date()): string {
  const et = new Date(
    now.toLocaleString("en-US", { timeZone: "America/New_York" }),
  );
  let left = Math.max(1, Math.round(tradingDays));
  while (left > 0) {
    et.setDate(et.getDate() + 1);
    const dow = et.getDay();
    if (dow !== 0 && dow !== 6) left -= 1;
  }
  const p = (n: number) => String(n).padStart(2, "0");
  return `${et.getFullYear()}-${p(et.getMonth() + 1)}-${p(et.getDate())}`;
}

/** Probability the TARGET is touched before the STOP for a driftless
 * (martingale) log-price — the gambler's-ruin answer, independent of vol
 * and of the time stop: ln(S/L) / ln(U/L) on the two barriers. Honest
 * baseline for the ticket's target chips: a 2R target is NOT a 33% shot,
 * it is whatever the log distances say. Null without both exits. */
export function pTargetFirst(exits: EquityExits): number | null {
  if (exits.tp == null) return null;
  const s = Math.abs(exits.entry);
  const stop = Math.abs(exits.sl);
  const target = Math.abs(exits.tp);
  if (!(s > 0 && stop > 0 && target > 0) || stop === target) return null;
  const p = Math.log(s / stop) / Math.log(target / stop);
  return Math.min(1, Math.max(0, p));
}

/** The ticket's entry price: marketable (ask long / bid short) when the
 * book is fresh, else the freshest spot; 0 when nothing prices. */
export function ticketPrice(
  quote: { bid: number; ask: number; mid: number } | null,
  quoteFresh: boolean,
  side: 1 | -1,
  fallbackSpot: number,
): number {
  const raw = side > 0 ? quote?.ask : quote?.bid;
  if (quoteFresh && raw && raw > 0) return raw;
  return fallbackSpot > 0 ? fallbackSpot : (quote?.mid ?? 0);
}
