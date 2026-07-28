/**
 * TS mirror of backend/app/services/options_math.py — the two MUST agree
 * (parity is asserted by vitest against fixtures generated from Python).
 *
 * Conventions (identical to Python):
 * - tau is in TRADING years: hours_to_expiry / (252 * 6.5)
 * - prices per share; position P/L multiplies by 100 * qty
 */

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
 * sorted by strike. Scenario IV uses STICKY MONEYNESS: the smile is a
 * function of K/S, so when spot moves from spot0 to S, a leg struck at K
 * reads the today-smile at the moneyness-equivalent strike K * spot0 / S.
 * With no usable smile (fewer than 2 points) it degrades to the leg's
 * frozen IV — classic sticky-strike BSM.
 */
export type SmilePoint = [number, number]; // [strike, iv]
export type Smiles = { C: SmilePoint[]; P: SmilePoint[] };

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
  const iv = smileIv(points, (leg.strike * spot0) / S);
  return iv !== null && iv > 0.005 ? iv : leg.iv;
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

export function payoffAtExpiry(legs: Leg[], S: number): number {
  let total = 0;
  for (const leg of legs) {
    const intrinsic = Math.max(leg.right === "C" ? S - leg.strike : leg.strike - S, 0);
    total += leg.side * leg.qty * intrinsic;
  }
  return total - positionEntryCost(legs);
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

export function breakevens(legs: Leg[], lo: number, hi: number, steps = 2000): number[] {
  const out: number[] = [];
  let prevS = lo;
  let prevV = payoffAtExpiry(legs, lo);
  for (let i = 1; i <= steps; i++) {
    const s = lo + ((hi - lo) * i) / steps;
    const v = payoffAtExpiry(legs, s);
    if (prevV === 0) out.push(prevS);
    else if (prevV < 0 !== v < 0) {
      let a = prevS;
      let b = s;
      for (let j = 0; j < 60; j++) {
        const m = 0.5 * (a + b);
        if (payoffAtExpiry(legs, m) < 0 === prevV < 0) a = m;
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
  const et = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const parts = Object.fromEntries(et.formatToParts(new Date(nowUtcMs)).map((p) => [p.type, p.value]));
  const hour = Number(parts.hour === "24" ? "0" : parts.hour);
  const nowMinutes = hour * 60 + Number(parts.minute) + Number(parts.second) / 60;
  const closeMinutes = 16 * 60;
  const hoursToday = Math.max(0, Math.min((closeMinutes - nowMinutes) / 60, 6.5));

  const todayIso = `${parts.year}-${parts.month}-${parts.day}`;
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
