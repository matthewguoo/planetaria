/**
 * TS mirror of backend/app/services/options_math.py — the two MUST agree
 * (parity is asserted by vitest against fixtures generated from Python).
 *
 * Conventions (identical to Python):
 * - tau is in TRADING years: hours_to_expiry / (252 * 6.5)
 * - prices per share; position P/L multiplies by 100 * qty
 */

import { etDateIso, etParts } from "./et";

export const TRADING_HOURS_PER_YEAR = 252 * 6.5;
export const RISK_FREE = 0.05;

export type Right = "C" | "P";
export type Leg = {
  right: Right;
  strike: number;
  qty: number;
  side: 1 | -1;
  entry: number; // premium per share at entry
  iv: number;
};

/**
 * Normal CDF via Marsaglia's Taylor series ("Evaluating the Normal
 * Distribution", JSS 2004): Phi(x) = 1/2 + pdf(x) * (x + x^3/3 + x^5/15 + ...)
 * All-positive terms for x >= 0 (no cancellation); double-precision agreement
 * with Python's erf-based cdf to <1e-13 across the clamped domain — parity
 * tests assert 1e-9.
 */
export function normCdf(x: number): number {
  if (x > 8) return 1;
  if (x < -8) return 0;
  if (x < 0) return 1 - normCdf(-x);
  let sum = x;
  let term = x;
  for (let k = 1; k < 300; k++) {
    term *= (x * x) / (2 * k + 1);
    sum += term;
    if (term < sum * 1e-17) break;
  }
  return 0.5 + sum * Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

export function normPdf(x: number): number {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

export function bsPrice(
  S: number,
  K: number,
  tau: number,
  sigma: number,
  right: Right,
  r: number = RISK_FREE,
): number {
  if (S <= 0) return right === "C" ? 0 : K * Math.exp(-r * Math.max(tau, 0));
  if (tau <= 0 || sigma <= 0) {
    const intrinsic = right === "C" ? S - K : K - S;
    return Math.max(intrinsic, 0);
  }
  const sq = sigma * Math.sqrt(tau);
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * tau) / sq;
  const d2 = d1 - sq;
  if (right === "C") return S * normCdf(d1) - K * Math.exp(-r * tau) * normCdf(d2);
  return K * Math.exp(-r * tau) * normCdf(-d2) - S * normCdf(-d1);
}

export function bsDelta(
  S: number,
  K: number,
  tau: number,
  sigma: number,
  right: Right,
  r: number = RISK_FREE,
): number {
  if (tau <= 0 || sigma <= 0 || S <= 0) {
    if (right === "C") return S > K ? 1 : 0;
    return S < K ? -1 : 0;
  }
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * tau) / (sigma * Math.sqrt(tau));
  return right === "C" ? normCdf(d1) : normCdf(d1) - 1;
}

/** BS theta per TRADING day (negative = decay), per share. Mirrors the
 * backend's bs_greeks theta_day. */
export function bsThetaPerDay(
  S: number,
  K: number,
  tau: number,
  sigma: number,
  right: Right,
  r: number = RISK_FREE,
): number {
  if (S <= 0 || tau <= 0 || sigma <= 0) return 0;
  const sq = sigma * Math.sqrt(tau);
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * tau) / sq;
  const d2 = d1 - sq;
  const decay = -(S * normPdf(d1) * sigma) / (2 * Math.sqrt(tau));
  const disc = Math.exp(-r * tau);
  const theta =
    right === "C" ? decay - r * K * disc * normCdf(d2) : decay + r * K * disc * normCdf(-d2);
  return theta / 252;
}

export function impliedVol(
  price: number,
  S: number,
  K: number,
  tau: number,
  right: Right,
  r: number = RISK_FREE,
): number | null {
  if (tau <= 0 || price <= 0 || S <= 0) return null;
  const intrinsic = Math.max(right === "C" ? S - K : K - S, 0);
  if (price <= intrinsic + 1e-10) return null;
  let lo = 1e-4;
  let hi = 10.0;
  if (bsPrice(S, K, tau, hi, right, r) < price) return null;
  for (let i = 0; i < 80; i++) {
    const mid = 0.5 * (lo + hi);
    if (bsPrice(S, K, tau, mid, right, r) < price) lo = mid;
    else hi = mid;
  }
  return 0.5 * (lo + hi);
}

/**
 * Volatility smile support — the post-BSM correction for "price moved, so
 * frozen-IV payoffs are wrong". points are [strike, iv] from the live chain,
 * sorted by strike. Scenario IV uses ANCHORED STICKY MONEYNESS: the smile is
 * a function of K/S, and the leg rides its SHAPE as a relative correction —
 *
 *     iv(S) = leg.iv + [ smile(K * spot0 / S) − smile(K) ]
 *
 * anchored to the leg's own entry-implied IV, never an absolute lookup. At
 * S = spot0 the correction is zero BY CONSTRUCTION, which pins the scenario
 * value at (now, spot) to the leg's actual market price. An absolute lookup
 * would inject phantom P/L at t=0 whenever the fitted smile disagrees with
 * the leg's own mid — guaranteed off-hours, when stale/crossed wing quotes
 * poison the fit (observed: a 1DTE call "instantly" worth 3x entry). With no
 * usable smile (fewer than 2 points) it degrades to the leg's frozen IV —
 * classic sticky-strike BSM.
 */
export type SmilePoint = [number, number]; // [strike, iv]
export type Smiles = { C: SmilePoint[]; P: SmilePoint[] };

/**
 * Robust smile: least-squares quadratic in log-moneyness through the raw
 * per-strike IV points, evaluated back onto the same strikes. Off-hours the
 * per-contract solves produce a SAWTOOTH (each strike solved from its own
 * junk quote); riding that noise through scenarioIv turns a smooth position
 * value into striped nonsense — especially with ratio legs. The quadratic
 * keeps the real skew (b) and curvature (c) and discards the noise. Falls
 * back to the raw points when there are too few for a stable fit.
 */
export function smoothSmile(points: SmilePoint[], spot: number): SmilePoint[] {
  if (points.length < 5 || spot <= 0) return points;
  const xs = points.map(([k]) => Math.log(k / spot));
  const ys = points.map(([, iv]) => iv);
  // Normal equations for y ~ a + b x + c x^2.
  let s0 = 0, s1 = 0, s2 = 0, s3 = 0, s4 = 0, t0 = 0, t1 = 0, t2 = 0;
  for (let i = 0; i < xs.length; i++) {
    const x = xs[i];
    const x2 = x * x;
    s0 += 1; s1 += x; s2 += x2; s3 += x2 * x; s4 += x2 * x2;
    t0 += ys[i]; t1 += ys[i] * x; t2 += ys[i] * x2;
  }
  const det =
    s0 * (s2 * s4 - s3 * s3) - s1 * (s1 * s4 - s3 * s2) + s2 * (s1 * s3 - s2 * s2);
  if (Math.abs(det) < 1e-12) return points;
  const a =
    (t0 * (s2 * s4 - s3 * s3) - s1 * (t1 * s4 - s3 * t2) + s2 * (t1 * s3 - s2 * t2)) / det;
  const b =
    (s0 * (t1 * s4 - t2 * s3) - t0 * (s1 * s4 - s3 * s2) + s2 * (s1 * t2 - t1 * s2)) / det;
  const c =
    (s0 * (s2 * t2 - s3 * t1) - s1 * (s1 * t2 - s3 * t0) + t0 * (s1 * s3 - s2 * s2)) / det;
  return points.map(([k]) => {
    const x = Math.log(k / spot);
    return [k, Math.max(a + b * x + c * x * x, 0.01)] as SmilePoint;
  });
}

export function smileIv(points: SmilePoint[], strike: number): number | null {
  if (points.length < 2) return null;
  if (strike <= points[0][0]) return points[0][1];
  if (strike >= points[points.length - 1][0]) return points[points.length - 1][1];
  for (let i = 1; i < points.length; i++) {
    if (strike <= points[i][0]) {
      const [k0, v0] = points[i - 1];
      const [k1, v1] = points[i];
      const frac = (strike - k0) / (k1 - k0 || 1);
      return v0 + frac * (v1 - v0);
    }
  }
  return points[points.length - 1][1];
}

export function scenarioIv(
  leg: Leg,
  S: number,
  spot0: number,
  smiles: Smiles | null,
): number {
  if (!smiles || S <= 0 || spot0 <= 0) return leg.iv;
  const points = leg.right === "C" ? smiles.C : smiles.P;
  const moved = smileIv(points, (leg.strike * spot0) / S);
  const anchor = smileIv(points, leg.strike);
  if (moved === null || anchor === null) return leg.iv;
  return Math.max(leg.iv + (moved - anchor), 0.005);
}

/**
 * Full scenario model = sticky-moneyness smile + two optional corrections:
 * - volShift: parallel relative IV shock (+0.2 = vols 20% richer) — the
 *   "what if IV crushes / spikes" axis BSM has no answer for.
 * - skew beta: a directional vol response derived from the chain's OWN skew
 *   slope (dIV/dlnK near ATM). Equity skews are negative, so a down-move
 *   raises scenario vols and a rally crushes them — the empirical index
 *   behavior that pure smile-riding misses.
 */
export type ScenarioModel = {
  spot0: number;
  smiles: Smiles | null;
  volShift: number;
  slopeC: number | null;
  slopeP: number | null;
};

/** Least-squares dIV/dlnK over smile points within ±6% of spot. */
export function atmSkewSlope(points: SmilePoint[], spot0: number): number | null {
  if (spot0 <= 0) return null;
  const pts = points.filter(
    ([k, v]) => k > 0 && v > 0 && Math.abs(Math.log(k / spot0)) < 0.06,
  );
  if (pts.length < 3) return null;
  let sx = 0;
  let sy = 0;
  let sxx = 0;
  let sxy = 0;
  for (const [k, v] of pts) {
    const x = Math.log(k);
    sx += x;
    sy += v;
    sxx += x * x;
    sxy += x * v;
  }
  const n = pts.length;
  const denom = n * sxx - sx * sx;
  if (Math.abs(denom) < 1e-12) return null;
  return (n * sxy - sx * sy) / denom;
}

export function makeScenarioModel(
  smiles: Smiles | null,
  spot0: number,
  volShift = 0,
  applySkewBeta = false,
): ScenarioModel {
  return {
    spot0,
    smiles,
    volShift,
    slopeC: applySkewBeta && smiles ? atmSkewSlope(smiles.C, spot0) : null,
    slopeP: applySkewBeta && smiles ? atmSkewSlope(smiles.P, spot0) : null,
  };
}

export function scenarioIvModel(leg: Leg, S: number, m: ScenarioModel): number {
  let iv = scenarioIv(leg, S, m.spot0, m.smiles);
  const slope = leg.right === "C" ? m.slopeC : m.slopeP;
  if (slope !== null && S > 0 && m.spot0 > 0) {
    iv += slope * Math.log(S / m.spot0);
  }
  iv *= 1 + m.volShift;
  return Math.min(Math.max(iv, 0.01), 5);
}

export function positionValueModel(
  legs: Leg[],
  S: number,
  tau: number,
  m: ScenarioModel,
  r: number = RISK_FREE,
): number {
  let total = 0;
  for (const leg of legs) {
    total += leg.side * leg.qty * bsPrice(S, leg.strike, tau, scenarioIvModel(leg, S, m), leg.right, r);
  }
  return total;
}

export function positionPlModel(
  legs: Leg[],
  S: number,
  tau: number,
  m: ScenarioModel,
  r: number = RISK_FREE,
): number {
  return positionValueModel(legs, S, tau, m, r) - positionEntryCost(legs);
}

/** Position value with smile-aware scenario vols (falls back to frozen IV). */
export function positionValueSmile(
  legs: Leg[],
  S: number,
  tau: number,
  spot0: number,
  smiles: Smiles | null,
  r: number = RISK_FREE,
): number {
  let total = 0;
  for (const leg of legs) {
    const iv = scenarioIv(leg, S, spot0, smiles);
    total += leg.side * leg.qty * bsPrice(S, leg.strike, tau, iv, leg.right, r);
  }
  return total;
}

export function positionPlSmile(
  legs: Leg[],
  S: number,
  tau: number,
  spot0: number,
  smiles: Smiles | null,
  r: number = RISK_FREE,
): number {
  return positionValueSmile(legs, S, tau, spot0, smiles, r) - positionEntryCost(legs);
}

export function positionValue(legs: Leg[], S: number, tau: number, r: number = RISK_FREE): number {
  let total = 0;
  for (const leg of legs) total += leg.side * leg.qty * bsPrice(S, leg.strike, tau, leg.iv, leg.right, r);
  return total;
}

export function positionEntryCost(legs: Leg[]): number {
  let total = 0;
  for (const leg of legs) total += leg.side * leg.qty * leg.entry;
  return total;
}

export function positionPl(legs: Leg[], S: number, tau: number, r: number = RISK_FREE): number {
  return positionValue(legs, S, tau, r) - positionEntryCost(legs);
}

/** Sum of leg intrinsic values (no basis) — the model-free expiry value. */
export function intrinsicValue(legs: Leg[], S: number): number {
  let total = 0;
  for (const leg of legs) {
    const intrinsic = Math.max(leg.right === "C" ? S - leg.strike : leg.strike - S, 0);
    total += leg.side * leg.qty * intrinsic;
  }
  return total;
}

/**
 * Expiry breakevens against an EXPLICIT premium basis (a live position's
 * actual fill differs from the legs' quoted mids). Model-free: intrinsic
 * payoff only, refined by bisection.
 */
export function breakevensForBasis(
  legs: Leg[],
  basis: number,
  lo: number,
  hi: number,
  steps = 1500,
): number[] {
  const f = (s: number) => intrinsicValue(legs, s) - basis;
  const out: number[] = [];
  let prevS = lo;
  let prevV = f(lo);
  for (let i = 1; i <= steps; i++) {
    const s = lo + ((hi - lo) * i) / steps;
    const v = f(s);
    if (prevV === 0) out.push(prevS);
    else if (prevV < 0 !== v < 0) {
      let a = prevS;
      let b = s;
      for (let j = 0; j < 60; j++) {
        const m = 0.5 * (a + b);
        if (f(m) < 0 === prevV < 0) a = m;
        else b = m;
      }
      out.push(0.5 * (a + b));
    }
    prevS = s;
    prevV = v;
  }
  const dedup: number[] = [];
  for (const be of out) {
    if (!dedup.length || Math.abs(be - dedup[dedup.length - 1]) > 0.01) {
      dedup.push(Math.round(be * 10000) / 10000);
    }
  }
  return dedup;
}

export function payoffAtExpiry(legs: Leg[], S: number): number {
  return intrinsicValue(legs, S) - positionEntryCost(legs);
}

/**
 * Worst-case loss per share if held to expiry (positive number); null =
 * unbounded (net short calls). Expiry payoff is piecewise linear with kinks
 * only at strikes, so kinks + endpoints are exact. Mirrors Python.
 */
export function structuralMaxLoss(legs: Leg[]): number | null {
  let slopeUp = 0;
  for (const leg of legs) if (leg.right === "C") slopeUp += leg.side * leg.qty;
  if (slopeUp < 0) return null;
  const strikes = legs.map((l) => l.strike);
  const points = [0, ...strikes, Math.max(...strikes) * 2];
  let worst = Infinity;
  for (const s of points) worst = Math.min(worst, payoffAtExpiry(legs, s));
  return -Math.min(worst, 0);
}

/**
 * Buying-power estimate per contract-set, broker-margin style.
 *
 * Defined-risk structures consume their structural max loss (spread margin).
 * Uncovered short units use the standard naked-option formula
 *   max(20% * spot - OTM amount, 10% * strike-or-spot floor) * 100
 * instead of full cash-secured/structural value — a short ITM SPY put would
 * otherwise "cost" $74k/set and zero out sizing that the enforced stop
 * actually bounds. Net debit is added when paid. An estimate, not the
 * broker's number: Alpaca applies its real margin at submit.
 */
/** Uncovered short units of one right (shorts beyond long coverage).
 * Alpaca L3 accounts cannot hold uncovered short CALLS at all — the broker
 * rejects the order ("account not eligible to trade uncovered option
 * contracts", verified empirically); uncovered short puts are accepted. */
export function nakedShortUnits(legs: Leg[], right: "C" | "P"): number {
  return Math.max(
    -legs.filter((l) => l.right === right).reduce((a, l) => a + l.side * l.qty, 0),
    0,
  );
}

export function bpPerSetEstimate(legs: Leg[], spot: number): number {
  const entry = positionEntryCost(legs);
  const debit = Math.max(entry, 0) * 100;
  const structural = structuralMaxLoss(legs);
  const nakedCalls = nakedShortUnits(legs, "C");
  const nakedPuts = nakedShortUnits(legs, "P");
  if (nakedCalls === 0 && nakedPuts === 0 && structural !== null) {
    return Math.max(structural * 100, debit);
  }
  let margin = 0;
  if (spot > 0) {
    const shortLegs = (right: "C" | "P") =>
      legs.filter((l) => l.right === right && l.side < 0);
    // Uncovered short puts: CASH-SECURED strike value. Broker-verified: an
    // oversized jade lizard probe was rejected with cost_basis $70,087/set
    // for a 700P — Alpaca charges ~strike x 100, NOT the 20% Reg-T formula.
    let putsLeft = nakedPuts;
    for (const leg of shortLegs("P").sort((a, b) => b.strike - a.strike)) {
      const units = Math.min(leg.qty, putsLeft);
      if (units <= 0) continue;
      putsLeft -= units;
      margin += units * leg.strike * 100;
    }
    // Uncovered short calls are unplaceable at this account level (guarded
    // upstream); the Reg-T formula stands in for display-only estimates.
    let callsLeft = nakedCalls;
    for (const leg of shortLegs("C").sort((a, b) => a.strike - b.strike)) {
      const units = Math.min(leg.qty, callsLeft);
      if (units <= 0) continue;
      callsLeft -= units;
      const otm = Math.max(leg.strike - spot, 0);
      margin += units * Math.max(0.2 * spot - otm, 0.1 * spot) * 100;
    }
  }
  // Covered residue (e.g. the spread part of a jade lizard) still consumes
  // its defined-risk margin when one exists.
  const coveredPart = structural !== null && nakedCalls === 0 && nakedPuts === 0 ? structural * 100 : 0;
  return Math.max(margin + debit + coveredPart, debit, 1);
}

export function breakevens(legs: Leg[], lo: number, hi: number, steps = 2000): number[] {
  // payoffAtExpiry(legs, s) === intrinsicValue(legs, s) − entry cost, so the
  // quoted-mid breakevens are the basis breakevens at the legs' own cost.
  return breakevensForBasis(legs, positionEntryCost(legs), lo, hi, steps);
}

export function probItm(
  S: number,
  K: number,
  tau: number,
  sigma: number,
  right: Right,
  r: number = RISK_FREE,
): number {
  if (tau <= 0 || sigma <= 0 || S <= 0) {
    if (right === "C") return S > K ? 1 : 0;
    return S < K ? 1 : 0;
  }
  const sq = sigma * Math.sqrt(tau);
  const d2 = (Math.log(S / K) + (r - 0.5 * sigma * sigma) * tau) / sq;
  return right === "C" ? normCdf(d2) : normCdf(-d2);
}

export function probAboveAtExpiry(
  S: number,
  level: number,
  tau: number,
  sigma: number,
  r: number = RISK_FREE,
): number {
  return probItm(S, level, tau, sigma, "C", r);
}

export function probTouch(
  S: number,
  barrier: number,
  tau: number,
  sigma: number,
  r: number = RISK_FREE,
): number {
  if (tau <= 0 || sigma <= 0 || S <= 0 || barrier <= 0) return 0;
  let b = Math.log(barrier / S);
  if (Math.abs(b) < 1e-12) return 1;
  let nu = r - 0.5 * sigma * sigma;
  if (b < 0) {
    b = -b;
    nu = -nu;
  }
  const sq = sigma * Math.sqrt(tau);
  const exponent = Math.min((2 * nu * b) / (sigma * sigma), 700);
  const p = normCdf((nu * tau - b) / sq) + Math.exp(exponent) * normCdf((-nu * tau - b) / sq);
  return Math.min(Math.max(p, 0), 1);
}

export function premiumBarrierUnderlying(
  legs: Leg[],
  targetPremium: number,
  tauEval: number,
  lo: number,
  hi: number,
  r: number = RISK_FREE,
): number | null {
  const f = (s: number) => positionValue(legs, s, tauEval, r) - targetPremium;
  const flo = f(lo);
  const fhi = f(hi);
  if (flo === 0) return lo;
  if (fhi === 0) return hi;
  if (flo < 0 === fhi < 0) return null;
  let a = lo;
  let b = hi;
  for (let i = 0; i < 80; i++) {
    const m = 0.5 * (a + b);
    if (f(m) < 0 === flo < 0) a = m;
    else b = m;
  }
  return 0.5 * (a + b);
}

export function terminalEv(
  legs: Leg[],
  S: number,
  tau: number,
  sigma: number,
  tpPremium: number | null,
  slPremium: number | null,
  r: number = RISK_FREE,
  steps = 400,
): number {
  if (tau <= 0 || sigma <= 0) return payoffAtExpiry(legs, S);
  const entry = positionEntryCost(legs);
  const nu = (r - 0.5 * sigma * sigma) * tau;
  const sq = sigma * Math.sqrt(tau);
  const loZ = -5;
  const hiZ = 5;
  const dz = (hiZ - loZ) / steps;
  let total = 0;
  for (let i = 0; i <= steps; i++) {
    const z = loZ + i * dz;
    const weight = normPdf(z) * dz * (i === 0 || i === steps ? 0.5 : 1);
    const sT = S * Math.exp(nu + sq * z);
    let pl = payoffAtExpiry(legs, sT);
    if (tpPremium !== null) pl = Math.min(pl, tpPremium - entry);
    if (slPremium !== null) pl = Math.max(pl, slPremium - entry);
    total += weight * pl;
  }
  return total;
}

export function positionIv(legs: Leg[]): number {
  const weights = legs.map((leg) => Math.abs(leg.qty * leg.entry));
  const total = weights.reduce((a, b) => a + b, 0);
  if (total <= 0) {
    const ivs = legs.filter((leg) => leg.iv > 0).map((leg) => leg.iv);
    return ivs.length ? ivs.reduce((a, b) => a + b, 0) / ivs.length : 0;
  }
  let acc = 0;
  legs.forEach((leg, i) => (acc += weights[i] * leg.iv));
  return acc / total;
}

/** Trading hours from now (ms) to expiry-day 16:00 ET — mirrors Python. */
export function tradingHoursToExpiry(expiryIso: string, nowUtcMs: number): number {
  const parts = etParts(nowUtcMs);
  const nowMinutes = parts.hour * 60 + parts.minute + parts.second / 60;
  const closeMinutes = 16 * 60;
  const hoursToday = Math.max(0, Math.min((closeMinutes - nowMinutes) / 60, 6.5));

  const todayIso = etDateIso(nowUtcMs);
  if (expiryIso === todayIso) return hoursToday;

  // Count weekdays strictly after today, up to and including expiry.
  let days = 0;
  const cursor = new Date(`${todayIso}T00:00:00Z`);
  const expiry = new Date(`${expiryIso}T00:00:00Z`);
  while (cursor < expiry) {
    cursor.setUTCDate(cursor.getUTCDate() + 1);
    const dow = cursor.getUTCDay();
    if (dow !== 0 && dow !== 6) days += 1;
  }
  return hoursToday + days * 6.5;
}
