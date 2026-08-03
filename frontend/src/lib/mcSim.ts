/**
 * Monte Carlo exit simulator: the honest answer to "what will this trade
 * actually realize?". The price×time surface is path-INdependent, but the
 * bracket exits are path-dependent — whether you reach (S, t) still holding
 * the position depends on whether TP/SL fired earlier. This engine simulates
 * GBM paths in trading time and applies the EXACT exit rules the server-side
 * enforcer uses (TP/SL on smile-aware position premium, hard time stop,
 * expiry payoff), with bid/ask friction, and reports the realized-P/L
 * distribution instead of a point estimate.
 *
 * Fill assumptions (mirrors the enforcer's mechanics, conservatively):
 * - TP fills AT the threshold (it's a limit — no positive slippage).
 * - SL fills at the simulated value when first observed at/past the stop —
 *   gap-through-stop within a step realizes the worse price, not the stop.
 * - Friction (round-trip half-spreads) is subtracted from every path.
 * Known assumptions: risk-neutral drift, diffusion only (no overnight jump
 * process), vols per the scenario model (smile + shock + skew beta).
 */

import {
  makeScenarioModel,
  payoffAtExpiry,
  positionIv,
  positionValueModel,
  smileIv,
  TRADING_HOURS_PER_YEAR,
  type Leg,
  type Smiles,
} from "./optionsMath";

export type McParams = {
  legs: Leg[];
  entry: number; // signed net premium per share
  tpPremium: number | null;
  slPremium: number | null;
  hoursToExpiry: number;
  timeStopHours: number; // trading hours from now until forced exit
  spot: number;
  smiles: Smiles | null;
  volShift: number;
  skewBeta: boolean;
  frictionPerSet: number; // dollars round-trip per contract-set
  paths: number;
  stepMinutes: number;
  seed: number;
};

export type McResult = {
  paths: number;
  evPerSet: number; // mean realized P/L, dollars/set (net of friction)
  winRate: number;
  pTp: number;
  pSl: number;
  pTime: number;
  pExpiry: number;
  avgMinutesInTrade: number;
  p5: number;
  p50: number;
  p95: number;
};

/** Deterministic RNG (mulberry32) so results are reproducible/testable. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gaussianPair(rng: () => number): [number, number] {
  let u = 0;
  while (u === 0) u = rng();
  const v = rng();
  const mag = Math.sqrt(-2 * Math.log(u));
  return [mag * Math.cos(2 * Math.PI * v), mag * Math.sin(2 * Math.PI * v)];
}

const R = 0.05;

export function simulateExits(p: McParams): McResult {
  const model = makeScenarioModel(p.smiles, p.spot, p.volShift, p.skewBeta);
  // Path volatility: scenario ATM vol (smile at spot, shocked), falling back
  // to the position's entry-weighted IV.
  const atm =
    (p.smiles && (smileIv(p.smiles.C, p.spot) ?? smileIv(p.smiles.P, p.spot))) ||
    positionIv(p.legs) ||
    0.2;
  const sigma = Math.min(Math.max(atm * (1 + p.volShift), 0.01), 5);

  const horizonH = Math.max(Math.min(p.timeStopHours, p.hoursToExpiry), 1 / 60);
  const stepH = p.stepMinutes / 60;
  const nSteps = Math.max(1, Math.ceil(horizonH / stepH));
  const atExpiry = p.timeStopHours >= p.hoursToExpiry - 1e-9;

  const pls = new Float64Array(p.paths);
  let sumMinutes = 0;
  let nTp = 0;
  let nSl = 0;
  let nTime = 0;
  let nExpiry = 0;
  let wins = 0;

  const rng = mulberry32(p.seed);
  let spare: number | null = null;
  const nextZ = () => {
    if (spare !== null) {
      const z = spare;
      spare = null;
      return z;
    }
    const [a, b] = gaussianPair(rng);
    spare = b;
    return a;
  };

  for (let path = 0; path < p.paths; path++) {
    let S = p.spot;
    let plShare: number | null = null;
    let exitMinutes = horizonH * 60;

    for (let i = 1; i <= nSteps; i++) {
      const tH = Math.min(i * stepH, horizonH);
      const dtH = tH - Math.min((i - 1) * stepH, horizonH);
      const dtY = dtH / TRADING_HOURS_PER_YEAR;
      S *= Math.exp((R - 0.5 * sigma * sigma) * dtY + sigma * Math.sqrt(dtY) * nextZ());
      const tauRem = Math.max(p.hoursToExpiry - tH, 0) / TRADING_HOURS_PER_YEAR;
      const value =
        tauRem <= 0
          ? payoffAtExpiry(p.legs, S) + p.entry // intrinsic, back to value axis
          : positionValueModel(p.legs, S, tauRem, model);
      if (p.tpPremium !== null && value >= p.tpPremium) {
        plShare = p.tpPremium - p.entry; // limit fill at the threshold
        exitMinutes = tH * 60;
        nTp++;
        break;
      }
      if (p.slPremium !== null && value <= p.slPremium) {
        plShare = value - p.entry; // stop: realize the (worse) observed value
        exitMinutes = tH * 60;
        nSl++;
        break;
      }
    }

    if (plShare === null) {
      if (atExpiry) {
        plShare = payoffAtExpiry(p.legs, S);
        nExpiry++;
      } else {
        const tauRem = Math.max(p.hoursToExpiry - horizonH, 0) / TRADING_HOURS_PER_YEAR;
        plShare = positionValueModel(p.legs, S, tauRem, model) - p.entry;
        nTime++;
      }
    }

    const pl = plShare * 100 - p.frictionPerSet;
    pls[path] = pl;
    sumMinutes += exitMinutes;
    if (pl > 0) wins++;
  }

  const sorted = Array.from(pls).sort((a, b) => a - b);
  const q = (frac: number) => sorted[Math.min(sorted.length - 1, Math.floor(frac * sorted.length))];
  const mean = sorted.reduce((a, b) => a + b, 0) / sorted.length;

  return {
    paths: p.paths,
    evPerSet: mean,
    winRate: wins / p.paths,
    pTp: nTp / p.paths,
    pSl: nSl / p.paths,
    pTime: nTime / p.paths,
    pExpiry: nExpiry / p.paths,
    avgMinutesInTrade: sumMinutes / p.paths,
    p5: q(0.05),
    p50: q(0.5),
    p95: q(0.95),
  };
}
