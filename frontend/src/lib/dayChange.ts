/**
 * Session change for the header: last price vs the previous session's
 * close, read off the chart's own bars (no extra endpoint, no broker
 * snapshot). The reference close is the last bar printed before 16:00 ET
 * on the most recent ET date strictly before the latest bar's date — so an
 * ETH tape's after-hours prints never masquerade as the close, and during
 * premarket / weekends (RTH-only tape, latest bar = yesterday's close) the
 * change is measured against that bar, the way phone brokers show it.
 */

import { etOffsetMinutes, etParts } from "./et";
import type { Bars } from "../components/Chart/scales";

const RTH_END_MIN = 16 * 60;

export type DayChange = { prevClose: number; change: number; pct: number };

function etDateKey(ms: number): string {
  const p = etParts(ms);
  return `${p.year}-${p.month}-${p.day}`;
}

/** Index of the reference close bar, or -1 when the tape is too short. */
export function prevCloseIndex(bars: Bars, nowMs: number = Date.now()): number {
  if (bars.n < 1) return -1;
  const lastKey = etDateKey(bars.t[bars.n - 1]);
  const todayKey = etDateKey(nowMs);
  // The tape ends on an earlier session (premarket, weekend, holiday): its
  // last bar IS the previous close.
  if (lastKey !== todayKey) return bars.n - 1;
  const etOff = etOffsetMinutes(bars.t[bars.n - 1]);
  for (let i = bars.n - 1; i >= 0; i--) {
    if (etDateKey(bars.t[i]) === lastKey) continue;
    const minuteOfDay = (((bars.t[i] / 60_000 + etOff) % 1440) + 1440) % 1440;
    if (minuteOfDay < RTH_END_MIN) return i;
  }
  return -1;
}

export function dayChange(bars: Bars, last: number, nowMs: number = Date.now()): DayChange | null {
  if (!(last > 0)) return null;
  const idx = prevCloseIndex(bars, nowMs);
  if (idx < 0) return null;
  const prevClose = bars.c[idx];
  if (!(prevClose > 0)) return null;
  const change = last - prevClose;
  return { prevClose, change, pct: change / prevClose };
}
