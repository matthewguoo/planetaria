/** Live quotes re-price a leg only while fresh and two-sided; otherwise the
 * chain-snapshot leg stands. */
import { describe, expect, it } from "vitest";
import { applyLiveQuotes, LEG_QUOTE_FRESH_MS } from "./liveLegs";
import { bsPrice } from "./optionsMath";
import type { StrategyLeg } from "../store/strategyStore";

const NOW = 1_785_400_020_000;
const tau = 4 / (252 * 6.5); // four trading hours, in years
const spot = 650;

const leg: StrategyLeg = {
  right: "C", strike: 650, qty: 1, side: 1, entry: 2.0, iv: 0.2,
  symbol: "SPY260904C00650000", expiry: "2026-09-04", halfSpread: 0.03,
};

describe("applyLiveQuotes", () => {
  it("re-prices entry, half-spread and IV from a fresh two-sided quote", () => {
    const mid = bsPrice(spot, 650, tau, 0.25, "C");
    const res = applyLiveQuotes([leg], {
      [leg.symbol]: { symbol: leg.symbol, bid: mid - 0.02, ask: mid + 0.02, mid, ts: NOW - 500 },
    }, spot, tau, NOW);
    expect(res.live).toBe(1);
    expect(res.ageMs).toBe(500);
    expect(res.legs[0].entry).toBeCloseTo(mid, 6);
    expect(res.legs[0].halfSpread).toBeCloseTo(0.02, 6);
    expect(res.legs[0].iv).toBeCloseTo(0.25, 2);
  });

  it("leaves the snapshot leg when the quote is stale, one-sided or missing", () => {
    const stale = applyLiveQuotes([leg], {
      [leg.symbol]: { symbol: leg.symbol, bid: 1.9, ask: 2.1, mid: 2.0, ts: NOW - LEG_QUOTE_FRESH_MS - 1 },
    }, spot, tau, NOW);
    expect(stale.live).toBe(0);
    expect(stale.legs[0]).toBe(leg);
    const oneSided = applyLiveQuotes([leg], {
      [leg.symbol]: { symbol: leg.symbol, bid: 0, ask: 2.1, mid: 2.1, ts: NOW },
    }, spot, tau, NOW);
    expect(oneSided.live).toBe(0);
    const missing = applyLiveQuotes([leg], {}, spot, tau, NOW);
    expect(missing.live).toBe(0);
    expect(missing.ageMs).toBeNull();
  });

  it("keeps the snapshot IV when the live mid has no BSM solution", () => {
    // Below intrinsic: no vol prices it.
    const res = applyLiveQuotes([leg], {
      [leg.symbol]: { symbol: leg.symbol, bid: 0.01, ask: 0.03, mid: 0.02, ts: NOW },
    }, 700, tau, NOW);
    expect(res.live).toBe(1);
    expect(res.legs[0].entry).toBe(0.02);
    expect(res.legs[0].iv).toBe(0.2);
  });
});
