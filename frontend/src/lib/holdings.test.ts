import { describe, expect, it } from "vitest";
import type { Holding } from "./api";
import { exposureOf, sortHoldings, summarize } from "./holdings";

const h = (underlying: string, extra: Partial<Holding>): Holding =>
  ({
    symbol: underlying,
    underlying,
    name: null,
    asset_class: "stock",
    qty: 100,
    side: 1,
    avg_entry_price: 10,
    current_price: 11,
    market_value: 1100,
    unrealized_pl: 100,
    unrealized_plpc: 0.1,
    unrealized_intraday_pl: 10,
    change_today: 0.01,
    lastday_price: 10.9,
    cost_basis: 1000,
    occ: null,
    plan_id: null,
    plan_status: null,
    sl: null,
    tp: null,
    time_stop_utc: null,
    protected: false,
    ...extra,
  }) as Holding;

const rows = [
  h("AAPX", { market_value: 2500, change_today: 0.012, unrealized_pl: 50, protected: true, sl: 20 }),
  h("PLTZ", { market_value: -900, change_today: -0.08, unrealized_pl: -300 }),
  h("SPCU", { market_value: 4000, change_today: 0.03, unrealized_pl: 400, unrealized_intraday_pl: 120 }),
];

describe("sortHoldings", () => {
  it("size = |market value| descending", () => {
    expect(sortHoldings(rows, "size", 10_000).map((r) => r.underlying)).toEqual(["SPCU", "AAPX", "PLTZ"]);
  });
  it("movers = |today's change| descending, sign-blind", () => {
    expect(sortHoldings(rows, "movers", 10_000).map((r) => r.underlying)).toEqual(["PLTZ", "SPCU", "AAPX"]);
  });
  it("pnl = signed dollars descending; name = alphabetical", () => {
    expect(sortHoldings(rows, "pnl", 10_000).map((r) => r.underlying)).toEqual(["SPCU", "AAPX", "PLTZ"]);
    expect(sortHoldings(rows, "name", 10_000).map((r) => r.underlying)).toEqual(["AAPX", "PLTZ", "SPCU"]);
  });
  it("exposure is |value| / equity", () => {
    expect(exposureOf(rows[1], 9_000)).toBeCloseTo(0.1, 9);
  });
});

describe("summarize", () => {
  it("rolls up invested, today's P/L, unrealized and protection", () => {
    const s = summarize(rows);
    expect(s.invested).toBe(7400);
    expect(s.todayPl).toBe(140);
    expect(s.unrealized).toBe(150);
    expect(s.protectedCount).toBe(1);
    expect(s.total).toBe(3);
  });
});
