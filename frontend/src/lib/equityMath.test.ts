import { describe, expect, it } from "vitest";
import type { ManualBook } from "./api";
import {
  capitalUsd,
  equityExits,
  equityPreflight,
  rr,
  sharesForRisk,
} from "./equityMath";

const BOOK: ManualBook = {
  enabled: true,
  equity_usd: 11_000,
  max_loss_pct: 0.02,
  per_trade_max_loss_usd: 220,
  daily_loss_usd: 330,
  max_open_plans: 4,
  require_stop_equity: true,
  open_plans: 0,
  used_usd: 0,
  remaining_usd: 11_000,
  realized_today: 0,
};

describe("equityExits — ref_tick's signed arithmetic, verbatim", () => {
  it("long: entry positive, sl below, tp above", () => {
    // entry = side*price; tp = side*price*(1+side*tp); sl = side*price*(1-side*sl)
    const { entry, sl, tp } = equityExits(100, 1, 0.05, 0.1);
    expect(entry).toBe(100);
    expect(sl).toBe(95);
    expect(tp).toBe(110);
  });

  it("short: entry negative, sl still below entry, tp still above", () => {
    const { entry, sl, tp } = equityExits(100, -1, 0.05, 0.1);
    expect(entry).toBe(-100);
    expect(sl).toBe(-105); // price rallies 5% against the short
    expect(tp).toBe(-90); // price drops 10% in favor
    expect(sl).toBeLessThan(entry);
    expect(tp!).toBeGreaterThan(entry);
  });

  it("no target -> tp null (run mode)", () => {
    expect(equityExits(50, 1, 0.03, null).tp).toBeNull();
  });
});

describe("sharesForRisk / rr / capitalUsd", () => {
  it("floors shares to whole units off the stop distance, both sides", () => {
    const long = equityExits(100, 1, 0.05, null);
    expect(sharesForRisk(110, long.entry, long.sl)).toBe(22); // 110 / 5
    const short = equityExits(100, -1, 0.05, null);
    expect(sharesForRisk(110, short.entry, short.sl)).toBe(22);
    expect(sharesForRisk(110, 100, 100)).toBe(0); // zero-distance stop
    expect(sharesForRisk(0, 100, 95)).toBe(0);
  });

  it("rr is reward over risk; null with no target", () => {
    const e = equityExits(100, 1, 0.05, 0.1);
    expect(rr(e.entry, e.tp, e.sl)).toBeCloseTo(2.0);
    expect(rr(e.entry, null, e.sl)).toBeNull();
  });

  it("shorts charge Reg-T 150%", () => {
    expect(capitalUsd(100, 10, 1)).toBe(1000);
    expect(capitalUsd(100, 10, -1)).toBe(1500);
  });
});

describe("equityPreflight mirrors the backend gates", () => {
  const base = (over: Partial<Parameters<typeof equityPreflight>[0]> = {}) => ({
    price: 100,
    side: 1 as const,
    shares: 20,
    exits: equityExits(100, 1 as const, 0.05, null),
    book: BOOK,
    equityLongOnly: true,
    quoteFresh: true,
    ...over,
  });

  it("clean long passes", () => {
    // 20 shares * $5 stop = $100 loss < $220 cap; $2k notional < $11k
    expect(equityPreflight(base())).toEqual([]);
  });

  it("short blocked while equity_long_only", () => {
    const r = equityPreflight(base({ side: -1, exits: equityExits(100, -1, 0.05, null) }));
    expect(r.some((x) => x.includes("shorts disabled"))).toBe(true);
  });

  it("per-trade max loss vs the BOOK, not the account", () => {
    const r = equityPreflight(base({ shares: 60 })); // 60 * $5 = $300 > $220
    expect(r.some((x) => x.includes("per-trade cap"))).toBe(true);
  });

  it("envelope: notional past remaining refused", () => {
    const book = { ...BOOK, remaining_usd: 1_000 };
    const r = equityPreflight(base({ book, shares: 20 })); // $2k > $1k left
    expect(r.some((x) => x.includes("remaining envelope"))).toBe(true);
  });

  it("max open plans + daily breaker", () => {
    const full = { ...BOOK, open_plans: 4 };
    expect(equityPreflight(base({ book: full })).some((x) => x.includes("open manual plans"))).toBe(true);
    const bled = { ...BOOK, realized_today: -331 };
    expect(equityPreflight(base({ book: bled })).some((x) => x.includes("daily loss breaker"))).toBe(true);
  });

  it("stale quote blocks", () => {
    const r = equityPreflight(base({ quoteFresh: false }));
    expect(r.some((x) => x.includes("no fresh quote"))).toBe(true);
  });
});
