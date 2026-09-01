import { describe, expect, it } from "vitest";
import {
  capitalUsd,
  dailySigmaPct,
  equityExits,
  equityPreflight,
  holdDaysUntil,
  holdSigmaPct,
  rr,
  sharesForRisk,
  suggestedStopPct,
} from "./equityMath";

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
    maxLossCapUsd: 220, // 2% of a real $11k account
    equityLongOnly: true,
    quoteFresh: true,
    ...over,
  });

  it("clean long passes", () => {
    // 20 shares * $5 stop = $100 loss < $220 cap
    expect(equityPreflight(base())).toEqual([]);
  });

  it("short blocked while equity_long_only", () => {
    const r = equityPreflight(base({ side: -1, exits: equityExits(100, -1, 0.05, null) }));
    expect(r.some((x) => x.includes("shorts disabled"))).toBe(true);
  });

  it("per-trade max loss vs the account cap", () => {
    const r = equityPreflight(base({ shares: 60 })); // 60 * $5 = $300 > $220
    expect(r.some((x) => x.includes("per-trade cap"))).toBe(true);
    // Unknown cap: check skipped (server still enforces).
    expect(equityPreflight(base({ shares: 60, maxLossCapUsd: null }))).toEqual([]);
  });

  it("stale quote blocks", () => {
    const r = equityPreflight(base({ quoteFresh: false }));
    expect(r.some((x) => x.includes("no fresh quote"))).toBe(true);
  });
});

describe("smart stop suggestions (vol-scaled)", () => {
  it("daily sigma from annualized RV", () => {
    // 32% annualized ≈ 2.0%/day (32 / sqrt(252))
    expect(dailySigmaPct(0.32)).toBeCloseTo(2.016, 2);
    expect(dailySigmaPct(0)).toBe(0);
  });

  it("hold sigma scales with sqrt(days)", () => {
    expect(holdSigmaPct(2, 4)).toBeCloseTo(4);
    expect(holdSigmaPct(2, 1)).toBeCloseTo(2);
  });

  it("suggested stop = 1.5x hold sigma, clamped and half-rounded", () => {
    expect(suggestedStopPct(2, 4)).toBe(6); // 1.5 * 4
    expect(suggestedStopPct(2, 1)).toBe(3);
    expect(suggestedStopPct(0.1, 1)).toBe(1); // floor clamp
    expect(suggestedStopPct(20, 25)).toBe(30); // ceiling clamp
    expect(suggestedStopPct(0, 5)).toBeNull(); // no vol data -> no claim
  });

  it("holdDaysUntil converts calendar to trading days with a floor", () => {
    const now = new Date("2026-09-01T12:00:00Z");
    expect(holdDaysUntil("2026-09-15", now)).toBe(10); // 14 cal -> 10 trading
    expect(holdDaysUntil("2026-09-01", now)).toBe(1); // floor
    expect(holdDaysUntil("garbage", now)).toBe(1);
  });
});
