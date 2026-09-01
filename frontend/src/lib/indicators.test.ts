import { describe, expect, it } from "vitest";
import type { Bars } from "../components/Chart/scales";
import {
  computeAtr,
  computeBollinger,
  computeEma,
  computeVwap,
  realizedVolAnnualized,
  sessionStarts,
} from "./indicators";
import { breakevensForBasis, type Leg } from "./optionsMath";

function mkBars(closes: number[], opts: { vol?: number[]; gapAt?: number } = {}): Bars {
  const n = closes.length;
  const t = new Float64Array(n);
  const v = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    t[i] = 1_700_000_000_000 + i * 60_000 + (opts.gapAt !== undefined && i >= opts.gapAt ? 4 * 3600 * 1000 : 0);
    v[i] = opts.vol?.[i] ?? 1000;
  }
  const c = Float64Array.from(closes);
  return { t, o: c.slice(), h: Float64Array.from(closes, (x) => x + 0.5), l: Float64Array.from(closes, (x) => x - 0.5), c, v, n };
}

describe("indicators", () => {
  it("detects session boundaries by time gaps", () => {
    const bars = mkBars([1, 2, 3, 4], { gapAt: 2 });
    const starts = sessionStarts(bars.t, bars.n);
    expect(Array.from(starts)).toEqual([1, 0, 1, 0]);
  });

  it("VWAP equals typical price under constant volume and resets per session", () => {
    const bars = mkBars([10, 20], { gapAt: 1 });
    const vwap = computeVwap(bars);
    expect(vwap[0]).toBeCloseTo(10, 9); // (10.5+9.5+10)/3
    expect(vwap[1]).toBeCloseTo(20, 9); // fresh session — no carryover
  });

  it("VWAP weights by volume within a session", () => {
    const bars = mkBars([10, 30], { vol: [3000, 1000] });
    const vwap = computeVwap(bars);
    expect(vwap[1]).toBeCloseTo((10 * 3000 + 30 * 1000) / 4000, 9);
  });

  it("EMA converges to a constant series", () => {
    const closes = new Float64Array(100).fill(50);
    const ema = computeEma(closes, 100, 9);
    expect(ema[99]).toBeCloseTo(50, 9);
  });

  it("Bollinger collapses to the mean for constant closes and widens with dispersion", () => {
    const flat = computeBollinger(mkBars(new Array(40).fill(100)));
    expect(flat.upper[39]).toBeCloseTo(100, 9);
    expect(flat.lower[39]).toBeCloseTo(100, 9);
    const noisy = computeBollinger(mkBars(Array.from({ length: 40 }, (_, i) => 100 + (i % 2 ? 2 : -2))));
    expect(noisy.upper[39]).toBeGreaterThan(noisy.lower[39] + 3);
  });

  it("ATR reflects bar ranges", () => {
    const atr = computeAtr(mkBars(new Array(30).fill(100)));
    expect(atr).toBeCloseTo(1.0, 6); // constant H-L = 1
  });

  it("realized vol is zero for constant closes and positive for noise", () => {
    expect(realizedVolAnnualized(mkBars(new Array(40).fill(100)), 30, 1)).toBe(0);
    const noisy = mkBars(Array.from({ length: 40 }, (_, i) => 100 * (1 + 0.001 * (i % 2 ? 1 : -1))));
    expect(realizedVolAnnualized(noisy, 30, 1)).toBeGreaterThan(0.1);
  });
});

describe("breakevensForBasis", () => {
  it("long call: BE = strike + basis", () => {
    const legs: Leg[] = [{ right: "C", strike: 455, qty: 1, side: 1, entry: 2, iv: 0.2 }];
    const bes = breakevensForBasis(legs, 2.0, 400, 500);
    expect(bes).toHaveLength(1);
    expect(bes[0]).toBeCloseTo(457, 2);
  });

  it("uses the EXPLICIT basis, not the legs' quoted mids", () => {
    const legs: Leg[] = [{ right: "C", strike: 455, qty: 1, side: 1, entry: 2, iv: 0.2 }];
    const bes = breakevensForBasis(legs, 3.5, 400, 500); // filled worse than mid
    expect(bes[0]).toBeCloseTo(458.5, 2);
  });

  it("iron condor: two breakevens around the body", () => {
    const legs: Leg[] = [
      { right: "P", strike: 440, qty: 1, side: 1, entry: 0.5, iv: 0.2 },
      { right: "P", strike: 445, qty: 1, side: -1, entry: 1.2, iv: 0.2 },
      { right: "C", strike: 460, qty: 1, side: -1, entry: 1.1, iv: 0.2 },
      { right: "C", strike: 465, qty: 1, side: 1, entry: 0.4, iv: 0.2 },
    ];
    const basis = -1.4; // net credit
    const bes = breakevensForBasis(legs, basis, 420, 485);
    expect(bes).toHaveLength(2);
    expect(bes[0]).toBeCloseTo(443.6, 2);
    expect(bes[1]).toBeCloseTo(461.4, 2);
  });
});
