/**
 * P/L surface computation off the main thread: grid + contour polylines.
 * Message in:  HeatmapRequest  ->  Message out: HeatmapResult
 * Values are dollars per contract-set (x100).
 *
 * Scenario pricing is SMILE-AWARE when the request carries the live chain
 * smile: as the scenario price moves away from spot, each leg is repriced
 * with a sticky-moneyness IV read off today's smile instead of its frozen
 * entry IV — the standard correction for BSM's "IV doesn't move when price
 * moves" blind spot. Contours (breakeven/TP/SL) are solved against the SAME
 * pricing function so the boundaries match the surface exactly.
 */

import {
  TRADING_HOURS_PER_YEAR,
  makeScenarioModel,
  positionEntryCost,
  positionValueModel,
  type Leg,
  type Smiles,
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
  /** current spot (smile anchor) and live smile; null => frozen-IV BSM */
  spot0: number;
  smiles: Smiles | null;
  /** relative IV shock (+0.2 = vols 20% richer) */
  volShift: number;
  /** apply skew-derived directional vol response */
  skewBeta: boolean;
  /** P/L basis override (position view: the plan's actual fill premium);
   * null/undefined => net of the legs' entry fields (designer). */
  entryOverride?: number | null;
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

/** Bisection solve of f(s) == 0 in [lo, hi]; NaN when no sign change. */
function solveCrossing(f: (s: number) => number, lo: number, hi: number): number {
  const flo = f(lo);
  const fhi = f(hi);
  if (flo === 0) return lo;
  if (fhi === 0) return hi;
  if (flo < 0 === fhi < 0) return NaN;
  let a = lo;
  let b = hi;
  for (let i = 0; i < 60; i++) {
    const m = 0.5 * (a + b);
    if (f(m) < 0 === flo < 0) a = m;
    else b = m;
  }
  return 0.5 * (a + b);
}

self.onmessage = (event: MessageEvent<HeatmapRequest>) => {
  const req = event.data;
  const { legs, priceLo, priceHi, priceSteps, timeSteps, hoursToExpiry, spot0, smiles } = req;
  const grid = new Float64Array(timeSteps * priceSteps);
  const breakevenLine = new Float64Array(timeSteps);
  const tpLine = new Float64Array(timeSteps);
  const slLine = new Float64Array(timeSteps);
  const entry = req.entryOverride ?? positionEntryCost(legs);

  const model = makeScenarioModel(smiles, spot0, req.volShift, req.skewBeta);
  const value = (s: number, tau: number) => positionValueModel(legs, s, tau, model);

  let minPl = Infinity;
  let maxPl = -Infinity;
  for (let ti = 0; ti < timeSteps; ti++) {
    // ti=0 is "now", last row is expiry.
    const hours = hoursToExpiry * (1 - ti / (timeSteps - 1));
    const tau = Math.max(hours, 0) / TRADING_HOURS_PER_YEAR;
    for (let pi = 0; pi < priceSteps; pi++) {
      const s = priceLo + ((priceHi - priceLo) * pi) / (priceSteps - 1);
      const pl = (value(s, tau) - entry) * 100;
      grid[ti * priceSteps + pi] = pl;
      if (pl < minPl) minPl = pl;
      if (pl > maxPl) maxPl = pl;
    }
    // Contours: underlying level where SMILE-AWARE position value crosses
    // each premium threshold at this tau.
    const solve = (target: number | null): number =>
      target === null ? NaN : solveCrossing((s) => value(s, tau) - target, priceLo, priceHi);
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
