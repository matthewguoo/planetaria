/**
 * P/L surface computation off the main thread: grid + contour polylines.
 * Message in:  HeatmapRequest  ->  Message out: HeatmapResult
 * Values are dollars per contract-set (x100).
 */

import {
  TRADING_HOURS_PER_YEAR,
  positionEntryCost,
  positionPl,
  positionValue,
  premiumBarrierUnderlying,
  type Leg,
} from "./optionsMath";

export type HeatmapRequest = {
  id: number;
  legs: Leg[];
  hoursToExpiry: number;
  priceLo: number;
  priceHi: number;
  priceSteps: number;
  timeSteps: number;
  tpPremium: number | null;
  slPremium: number | null;
  riskDollars: number; // per contract-set, for R normalization
};

export type HeatmapResult = {
  id: number;
  priceLo: number;
  priceHi: number;
  priceSteps: number;
  timeSteps: number;
  hoursToExpiry: number;
  /** row-major [timeSteps][priceSteps], dollars per contract-set */
  grid: Float64Array;
  minPl: number;
  maxPl: number;
  /** per time column: underlying price of each contour (NaN = none) */
  breakevenLine: Float64Array;
  tpLine: Float64Array;
  slLine: Float64Array;
};

self.onmessage = (event: MessageEvent<HeatmapRequest>) => {
  const req = event.data;
  const { legs, priceLo, priceHi, priceSteps, timeSteps, hoursToExpiry } = req;
  const grid = new Float64Array(timeSteps * priceSteps);
  const breakevenLine = new Float64Array(timeSteps);
  const tpLine = new Float64Array(timeSteps);
  const slLine = new Float64Array(timeSteps);
  const entry = positionEntryCost(legs);

  let minPl = Infinity;
  let maxPl = -Infinity;
  for (let ti = 0; ti < timeSteps; ti++) {
    // ti=0 is "now", last row is expiry.
    const hours = hoursToExpiry * (1 - ti / (timeSteps - 1));
    const tau = Math.max(hours, 0) / TRADING_HOURS_PER_YEAR;
    for (let pi = 0; pi < priceSteps; pi++) {
      const s = priceLo + ((priceHi - priceLo) * pi) / (priceSteps - 1);
      const pl = positionPl(legs, s, tau) * 100;
      grid[ti * priceSteps + pi] = pl;
      if (pl < minPl) minPl = pl;
      if (pl > maxPl) maxPl = pl;
    }
    // Contours: solve underlying level where position value crosses each
    // premium threshold at this tau.
    const solve = (target: number | null): number => {
      if (target === null) return NaN;
      const s = premiumBarrierUnderlying(legs, target, tau, priceLo, priceHi);
      return s === null ? NaN : s;
    };
    breakevenLine[ti] = solve(entry);
    tpLine[ti] = solve(req.tpPremium);
    slLine[ti] = solve(req.slPremium);
  }

  const result: HeatmapResult = {
    id: req.id,
    priceLo,
    priceHi,
    priceSteps,
    timeSteps,
    hoursToExpiry,
    grid,
    minPl,
    maxPl,
    breakevenLine,
    tpLine,
    slLine,
  };
  (self as unknown as Worker).postMessage(result, [
    grid.buffer,
    breakevenLine.buffer,
    tpLine.buffer,
    slLine.buffer,
  ]);
};
