/**
 * Shared chart state for HTML layers that live OUTSIDE the canvas component:
 * the sidebar HUD (needs bars for ATR/RV) and the leg rail (needs the live
 * y-scale to position pills). CandlePane publishes on every draw; consumers
 * subscribe without forcing React renders of the canvas itself.
 */

import type { Bars } from "../components/Chart/scales";

/** Live bars of the focused (symbol, tf) — written by CandlePane's pull. */
export const sharedBars: { current: Bars } = {
  current: { t: new Float64Array(0), o: new Float64Array(0), h: new Float64Array(0),
             l: new Float64Array(0), c: new Float64Array(0), v: new Float64Array(0), n: 0 },
};

export type ChartScale = {
  lo: number;      // price at bottom of the candle area
  hi: number;      // price at top
  volTopPx: number; // css px height of the candle area (price->y maps into this)
};

let scale: ChartScale | null = null;
let version = 0;
const listeners = new Set<() => void>();

export function publishScale(next: ChartScale): void {
  if (
    scale &&
    Math.abs(scale.lo - next.lo) < 1e-9 &&
    Math.abs(scale.hi - next.hi) < 1e-9 &&
    scale.volTopPx === next.volTopPx
  ) {
    return;
  }
  scale = next;
  version += 1;
  for (const listener of listeners) listener();
}

export function getScale(): ChartScale | null {
  return scale;
}

export function getScaleVersion(): number {
  return version;
}

export function subscribeScale(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function priceToRailY(price: number, s: ChartScale): number {
  return ((s.hi - price) / (s.hi - s.lo || 1)) * s.volTopPx;
}

export function railYToPrice(y: number, s: ChartScale): number {
  return s.hi - (y / s.volTopPx) * (s.hi - s.lo);
}
