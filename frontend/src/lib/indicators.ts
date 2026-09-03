/**
 * Intraday indicator math over bar arrays — the MFT context layer: where is
 * price vs the session's volume anchor (VWAP), short-term trend (EMAs),
 * dispersion (Bollinger), range (ATR), and — critical for options — REALIZED
 * vol to compare against the chain's implied vol.
 *
 * Sessions are detected by time gaps (> 3h between bars = new session), so
 * VWAP anchors per trading day without timezone bookkeeping.
 */

import type { Bars } from "../components/Chart/scales";

const SESSION_GAP_MS = 3 * 3600 * 1000;

export function sessionStarts(t: Float64Array, n: number): Uint8Array {
  const starts = new Uint8Array(n);
  if (n > 0) starts[0] = 1;
  for (let i = 1; i < n; i++) {
    if (t[i] - t[i - 1] > SESSION_GAP_MS) starts[i] = 1;
  }
  return starts;
}

/** Session-anchored VWAP from typical price (H+L+C)/3, volume-weighted. */
export function computeVwap(bars: Bars): Float64Array {
  const out = new Float64Array(bars.n);
  const starts = sessionStarts(bars.t, bars.n);
  let pv = 0;
  let vol = 0;
  for (let i = 0; i < bars.n; i++) {
    if (starts[i]) {
      pv = 0;
      vol = 0;
    }
    const typical = (bars.h[i] + bars.l[i] + bars.c[i]) / 3;
    pv += typical * bars.v[i];
    vol += bars.v[i];
    out[i] = vol > 0 ? pv / vol : typical;
  }
  return out;
}

export function computeEma(closes: Float64Array, n: number, period: number): Float64Array {
  const out = new Float64Array(n);
  if (!n) return out;
  const k = 2 / (period + 1);
  out[0] = closes[0];
  for (let i = 1; i < n; i++) out[i] = closes[i] * k + out[i - 1] * (1 - k);
  return out;
}

export type Bollinger = { mid: Float64Array; upper: Float64Array; lower: Float64Array };

export function computeBollinger(bars: Bars, period = 20, mult = 2): Bollinger {
  const n = bars.n;
  const mid = new Float64Array(n);
  const upper = new Float64Array(n);
  const lower = new Float64Array(n);
  let sum = 0;
  let sumSq = 0;
  for (let i = 0; i < n; i++) {
    const c = bars.c[i];
    sum += c;
    sumSq += c * c;
    if (i >= period) {
      const old = bars.c[i - period];
      sum -= old;
      sumSq -= old * old;
    }
    const len = Math.min(i + 1, period);
    const mean = sum / len;
    const variance = Math.max(sumSq / len - mean * mean, 0);
    const sd = Math.sqrt(variance);
    mid[i] = mean;
    upper[i] = mean + mult * sd;
    lower[i] = mean - mult * sd;
  }
  return { mid, upper, lower };
}

/** Wilder-smoothed ATR; returns the latest value (a range/stop-width read). */
export function computeAtr(bars: Bars, period = 14): number {
  const n = bars.n;
  if (n < 2) return 0;
  let atr = 0;
  let count = 0;
  for (let i = 1; i < n; i++) {
    const tr = Math.max(
      bars.h[i] - bars.l[i],
      Math.abs(bars.h[i] - bars.c[i - 1]),
      Math.abs(bars.l[i] - bars.c[i - 1]),
    );
    if (count < period) {
      atr += tr;
      count++;
      if (count === period) atr /= period;
    } else {
      atr = (atr * (period - 1) + tr) / period;
    }
  }
  return count < period ? atr / Math.max(count, 1) : atr;
}

/**
 * Annualized realized volatility from the last `lookback` bars' log returns.
 * Scaled by bar timeframe (tfMinutes) against 252 x 390-minute sessions —
 * the number to hold against ATM implied vol before paying premium.
 */
export function realizedVolAnnualized(bars: Bars, lookback: number, tfMinutes: number): number {
  const n = bars.n;
  if (n < 3) return 0;
  const start = Math.max(1, n - lookback);
  const rets: number[] = [];
  for (let i = start; i < n; i++) {
    if (bars.c[i - 1] > 0 && bars.c[i] > 0) rets.push(Math.log(bars.c[i] / bars.c[i - 1]));
  }
  if (rets.length < 2) return 0;
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const variance = rets.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (rets.length - 1);
  const barsPerYear = (252 * 390) / tfMinutes;
  return Math.sqrt(variance * barsPerYear);
}

/** Simple moving average over closes; the first `period-1` values use the
 * partial window (a line that starts at bar 0, not NaN for 200 bars). */
export function computeSma(closes: Float64Array, n: number, period: number): Float64Array {
  const out = new Float64Array(n);
  let sum = 0;
  for (let i = 0; i < n; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    out[i] = sum / Math.min(i + 1, period);
  }
  return out;
}

/** Wilder RSI over closes. Flat tape (no gains, no losses) reads 50. */
export function computeRsi(closes: Float64Array, n: number, period = 14): Float64Array {
  const out = new Float64Array(n);
  if (!n) return out;
  out[0] = 50;
  let avgGain = 0;
  let avgLoss = 0;
  for (let i = 1; i < n; i++) {
    const d = closes[i] - closes[i - 1];
    const gain = d > 0 ? d : 0;
    const loss = d < 0 ? -d : 0;
    if (i <= period) {
      avgGain += gain / period;
      avgLoss += loss / period;
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
    }
    out[i] =
      avgGain === 0 && avgLoss === 0 ? 50 : avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

export type Macd = { macd: Float64Array; signal: Float64Array; hist: Float64Array };

/** MACD (fast EMA − slow EMA), its signal EMA, and the histogram. */
export function computeMacd(
  closes: Float64Array,
  n: number,
  fast = 12,
  slow = 26,
  signalPeriod = 9,
): Macd {
  const f = computeEma(closes, n, fast);
  const s = computeEma(closes, n, slow);
  const macd = new Float64Array(n);
  for (let i = 0; i < n; i++) macd[i] = f[i] - s[i];
  const signal = computeEma(macd, n, signalPeriod);
  const hist = new Float64Array(n);
  for (let i = 0; i < n; i++) hist[i] = macd[i] - signal[i];
  return { macd, signal, hist };
}
