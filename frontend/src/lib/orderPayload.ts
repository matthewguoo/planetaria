/**
 * The options order body, built ONCE for every ticket (desktop ORDER panel,
 * phone ticket). What the server holds you to is exactly what the designer
 * shows: legs at their modelled entries with measured half-spreads, the
 * limit at the mid, TP/SL as premium levels, and the time stop in UTC.
 */

import { etDateIso, etWallToUtcIso } from "./et";
import type { Designer } from "./useDesigner";

/** Today at HH:MM ET -> UTC ISO. */
export function etToUtcIso(timeEt: string): string {
  return etWallToUtcIso(etDateIso(), timeEt);
}

export function optionsOrderPayload(args: {
  designer: Designer;
  symbol: string;
  kind: string;
  modified: boolean;
  timeStopEt: string;
}): object {
  const { designer, symbol, kind, modified, timeStopEt } = args;
  return {
    underlying: symbol,
    strategy: modified ? "custom" : kind,
    legs: designer.legs!.map((leg) => ({
      symbol: leg.symbol,
      right: leg.right,
      strike: leg.strike,
      expiry: leg.expiry,
      side: leg.side,
      ratio: leg.qty,
      entry: leg.entry,
      iv: leg.iv,
      half_spread: leg.halfSpread,
    })),
    qty: designer.qty,
    entry_limit: Number(designer.entry.toFixed(2)),
    tp_premium: Number(designer.tpPremium!.toFixed(2)),
    sl_premium: Number(designer.slPremium!.toFixed(2)),
    time_stop_utc: etToUtcIso(timeStopEt),
  };
}

/** The live account is options level 2: long single-leg only. Mirrors the
 * server's refusal so the ticket can show the reason before the 422. */
export function liveLevel2Blocked(live: boolean, designer: Designer): boolean {
  return (
    live &&
    !!designer.legs &&
    (designer.legs.length !== 1 || designer.legs.some((leg) => leg.side < 0))
  );
}
