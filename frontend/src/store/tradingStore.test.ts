/** freshSpot: the frontend half of the frozen-quote guard (mirrors
 * backend/tests/test_spot.py's freshest_spot semantics at the UI layer). */
import { describe, expect, it } from "vitest";
import { freshSpot, quoteIsStale, QUOTE_FRESH_MS, type Quote } from "./tradingStore";

const NOW = 1_785_400_000_000;

const quote = (mid: number, ageMs: number): Quote => ({
  symbol: "SPY",
  bid: mid - 0.1,
  ask: mid + 0.1,
  mid,
  ts: NOW - ageMs,
});

describe("freshSpot", () => {
  it("fresh quote wins over chain spot", () => {
    expect(freshSpot(quote(741.0, 5_000), 743.5, NOW)).toBe(741.0);
  });

  it("stale quote loses to chain spot (frozen overnight book)", () => {
    expect(freshSpot(quote(741.04, 16 * 3600_000), 743.56, NOW)).toBe(743.56);
  });

  it("boundary: just inside freshness window still wins", () => {
    expect(freshSpot(quote(741.0, QUOTE_FRESH_MS - 1), 743.5, NOW)).toBe(741.0);
  });

  it("no fallback: stale quote is still better than nothing", () => {
    expect(freshSpot(quote(741.0, 3600_000), 0, NOW)).toBe(741.0);
  });

  it("zero-mid quote ignored", () => {
    expect(freshSpot(quote(0, 1_000), 743.5, NOW)).toBe(743.5);
  });

  it("nothing at all -> 0", () => {
    expect(freshSpot(null, 0, NOW)).toBe(0);
  });
});

describe("quoteIsStale", () => {
  it("null quote is not stale (it is absent)", () => {
    expect(quoteIsStale(null, NOW)).toBe(false);
  });
  it("old quote is stale", () => {
    expect(quoteIsStale(quote(741, QUOTE_FRESH_MS + 1), NOW)).toBe(true);
  });
});
