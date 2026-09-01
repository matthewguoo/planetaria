/**
 * Live options chain for the active expiry. Every row is a real contract
 * pair from the chain API; clicking B/S on the call or put side adds that
 * contract as a leg (buy = +1, sell = −1) to the working position — stacking
 * ratio on repeats, capped at 4 legs (the MLEG order limit). Strikes already
 * in the position are highlighted with their side/ratio.
 */

import { useEffect, useRef } from "react";
import { nearestStrike, useStrategyStore } from "../../store/strategyStore";
import { useTradingStore } from "../../store/tradingStore";

export function ChainPanel() {
  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const strikes = useStrategyStore((s) => s.strikes);
  const rights = useStrategyStore((s) => s.rights);
  const sides = useStrategyStore((s) => s.sides);
  const ratios = useStrategyStore((s) => s.ratios);
  const addLeg = useStrategyStore((s) => s.addLeg);
  const quote = useTradingStore((s) => s.quote);
  const atmRef = useRef<HTMLTableRowElement>(null);
  const scrolledRef = useRef<string | null>(null);

  const spot = quote?.mid || chain?.spot || 0;
  const rows = (() => {
    if (!chain || !expiry) return [];
    const byStrike = new Map<number, { call?: (typeof chain.contracts)[number]; put?: (typeof chain.contracts)[number] }>();
    for (const c of chain.contracts) {
      if (c.expiry !== expiry) continue;
      const entry = byStrike.get(c.strike) ?? {};
      if (c.right === "C") entry.call = c;
      else entry.put = c;
      byStrike.set(c.strike, entry);
    }
    return [...byStrike.entries()].sort((a, b) => a[0] - b[0]);
  })();

  const atmStrike = rows.length ? nearestStrike(rows.map(([k]) => k), spot) : null;

  // Scroll to ATM once per (symbol, expiry).
  useEffect(() => {
    const key = `${chain?.underlying}:${expiry}`;
    if (scrolledRef.current !== key && atmRef.current) {
      atmRef.current.scrollIntoView({ block: "center" });
      scrolledRef.current = key;
    }
  }, [chain?.underlying, expiry, rows.length]);

  const legAt = (right: "C" | "P", strike: number) => {
    const i = strikes.findIndex((k, idx) => k === strike && rights[idx] === right);
    return i >= 0 ? { side: sides[i], ratio: ratios[i] ?? 1 } : null;
  };

  const bs = (right: "C" | "P", strike: number, disabled: boolean) => (
    <span className="inline-flex gap-px">
      <button
        className="border border-bb-border px-1 text-[9px] text-bb-profit hover:bg-bb-profit hover:text-black disabled:opacity-30"
        disabled={disabled}
        title={`Buy ${strike}${right} (adds +1 leg)`}
        onClick={() => addLeg({ right, side: 1, strike })}
      >
        B
      </button>
      <button
        className="border border-bb-border px-1 text-[9px] text-bb-loss hover:bg-bb-loss hover:text-black disabled:opacity-30"
        disabled={disabled}
        title={`Sell ${strike}${right} (adds −1 leg)`}
        onClick={() => addLeg({ right, side: -1, strike })}
      >
        S
      </button>
    </span>
  );

  if (!chain || !expiry) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-bb-muted">
        loading chain…
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-bb-border px-2 py-1 text-[10px] text-bb-muted">
        <span>CALLS</span>
        <span className="tracking-widest">{expiry.slice(5)}</span>
        <span>PUTS</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <table className="w-full border-collapse text-[10px]">
          <thead className="sticky top-0 bg-bb-panel text-[9px] text-bb-muted">
            <tr>
              <th className="px-1 py-0.5"></th>
              <th className="px-1 py-0.5 text-right">MID</th>
              <th className="px-1 py-0.5 text-right">IV</th>
              <th className="px-1 py-0.5 text-center">STRIKE</th>
              <th className="px-1 py-0.5 text-left">IV</th>
              <th className="px-1 py-0.5 text-left">MID</th>
              <th className="px-1 py-0.5"></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([strike, { call, put }]) => {
              const callLeg = legAt("C", strike);
              const putLeg = legAt("P", strike);
              const isAtm = strike === atmStrike;
              return (
                <tr
                  key={strike}
                  ref={isAtm ? atmRef : undefined}
                  className={
                    "border-b border-bb-border/40 " +
                    (isAtm ? "bg-bb-hover/60" : "hover:bg-bb-hover/40")
                  }
                >
                  <td className="px-1 py-0.5">{call && bs("C", strike, false)}</td>
                  <td
                    data-numeric
                    className={
                      "px-1 py-0.5 text-right " +
                      (callLeg ? "font-semibold text-bb-amber" : strike < spot ? "text-white" : "text-bb-muted")
                    }
                    title={call ? `bid ${call.bid.toFixed(2)} × ask ${call.ask.toFixed(2)}` : ""}
                  >
                    {call ? call.mid.toFixed(2) : "—"}
                    {callLeg && ` ${callLeg.side > 0 ? "+" : "−"}${callLeg.ratio}`}
                  </td>
                  <td data-numeric className="px-1 py-0.5 text-right text-bb-muted">
                    {call && call.iv > 0 ? Math.round(call.iv * 100) : "—"}
                  </td>
                  <td
                    data-numeric
                    className={
                      "px-1 py-0.5 text-center " + (isAtm ? "text-bb-amber" : "text-white")
                    }
                  >
                    {strike}
                  </td>
                  <td data-numeric className="px-1 py-0.5 text-left text-bb-muted">
                    {put && put.iv > 0 ? Math.round(put.iv * 100) : "—"}
                  </td>
                  <td
                    data-numeric
                    className={
                      "px-1 py-0.5 text-left " +
                      (putLeg ? "font-semibold text-bb-amber" : strike > spot ? "text-white" : "text-bb-muted")
                    }
                    title={put ? `bid ${put.bid.toFixed(2)} × ask ${put.ask.toFixed(2)}` : ""}
                  >
                    {put ? put.mid.toFixed(2) : "—"}
                    {putLeg && ` ${putLeg.side > 0 ? "+" : "−"}${putLeg.ratio}`}
                  </td>
                  <td className="px-1 py-0.5">{put && bs("P", strike, false)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="shrink-0 border-t border-bb-border px-2 py-0.5 text-[9px] text-bb-muted">
        B/S adds a leg to the working position · max 4 legs
      </div>
    </div>
  );
}
