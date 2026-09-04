/**
 * Overlay live option NBBO onto the ticket's legs. The chain snapshot
 * prices a leg every poll (2-10 s); a streamed quote re-prices it on every
 * tick: entry = live mid, half-spread from the live book, IV re-solved so
 * the model value at spot still equals the price (the same convention
 * buildLegs uses). A quote older than `freshMs` is ignored and the snapshot
 * leg stands, so a dead stream degrades to today's behaviour, never to a
 * frozen price.
 */

import { impliedVol } from "./optionsMath";
import type { StrategyLeg } from "../store/strategyStore";
import type { OptionQuote } from "../store/optionQuoteStore";

export const LEG_QUOTE_FRESH_MS = 15_000;

export type LiveLegs = {
  legs: StrategyLeg[];
  /** Legs priced off a fresh streamed quote (0 = all from the snapshot). */
  live: number;
  /** Age of the oldest fresh quote used, ms; null when none was used. */
  ageMs: number | null;
};

export function applyLiveQuotes(
  legs: StrategyLeg[],
  quotes: Record<string, OptionQuote>,
  spot: number,
  tauYears: number,
  nowMs: number = Date.now(),
  freshMs: number = LEG_QUOTE_FRESH_MS,
): LiveLegs {
  let live = 0;
  let ageMs: number | null = null;
  const out = legs.map((leg) => {
    const q = quotes[leg.symbol];
    if (!q || !(q.bid > 0) || !(q.ask >= q.bid) || !(q.mid > 0)) return leg;
    const age = nowMs - q.ts;
    if (age < 0 || age > freshMs) return leg;
    live += 1;
    ageMs = ageMs === null ? age : Math.max(ageMs, age);
    const solved =
      spot > 0 && tauYears > 0 ? impliedVol(q.mid, spot, leg.strike, tauYears, leg.right) : null;
    const iv = solved !== null && solved > 0.005 && solved < 5 ? solved : leg.iv;
    return { ...leg, entry: q.mid, iv, halfSpread: Math.max((q.ask - q.bid) / 2, 0) };
  });
  return { legs: out, live, ageMs };
}
