/**
 * Holdings table math: the sort keys a phone brokerage offers (size,
 * exposure, movers, P/L, name) and the account rollups the overview shows
 * above the table. Pure, so the ordering is tested rather than eyeballed.
 */

import type { Holding } from "./api";

export type HoldingsSort = "size" | "exposure" | "movers" | "pnl" | "name";

export const SORT_LABEL: Record<HoldingsSort, string> = {
  size: "SIZE",
  exposure: "EXPOSURE",
  movers: "MOVERS",
  pnl: "P/L",
  name: "A–Z",
};

/** Absolute dollars the position represents (options: contract value). */
export function holdingValue(h: Holding): number {
  return Math.abs(h.market_value ?? 0);
}

/** Share of the account, 0–1, on the position's market value. */
export function exposureOf(h: Holding, equity: number): number {
  return equity > 0 ? holdingValue(h) / equity : 0;
}

export function sortHoldings(rows: Holding[], by: HoldingsSort, equity: number): Holding[] {
  const key = (h: Holding): number | string => {
    switch (by) {
      case "size":
        return holdingValue(h);
      case "exposure":
        return exposureOf(h, equity);
      case "movers":
        return Math.abs(h.change_today ?? 0);
      case "pnl":
        return h.unrealized_pl ?? 0;
      case "name":
        return h.underlying;
    }
  };
  return [...rows].sort((a, b) => {
    const ka = key(a);
    const kb = key(b);
    if (typeof ka === "string" || typeof kb === "string") return String(ka).localeCompare(String(kb));
    return kb - ka || a.underlying.localeCompare(b.underlying);
  });
}

export type HoldingsSummary = {
  /** Sum of |market value| over every position. */
  invested: number;
  /** Broker's intraday P/L across positions (today's move on what you hold). */
  todayPl: number;
  unrealized: number;
  protectedCount: number;
  /** Long options with no stop: loss capped at the premium paid. */
  premiumCappedCount: number;
  total: number;
};

export function summarize(rows: Holding[]): HoldingsSummary {
  return rows.reduce<HoldingsSummary>(
    (acc, h) => ({
      invested: acc.invested + holdingValue(h),
      todayPl: acc.todayPl + (h.unrealized_intraday_pl ?? 0),
      unrealized: acc.unrealized + (h.unrealized_pl ?? 0),
      protectedCount: acc.protectedCount + (h.protected ? 1 : 0),
      premiumCappedCount: acc.premiumCappedCount + (h.protection === "premium" ? 1 : 0),
      total: acc.total + 1,
    }),
    { invested: 0, todayPl: 0, unrealized: 0, protectedCount: 0, premiumCappedCount: 0, total: 0 },
  );
}
