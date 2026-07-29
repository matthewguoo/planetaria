/**
 * Header price readout with the frozen-quote guard: the big number is the
 * FRESHEST spot (live quote mid, else chain/tape spot), and a quote whose
 * timestamp has gone stale is dimmed and flagged instead of masquerading as
 * live top-of-book. A 15s tick re-evaluates staleness even when no new
 * quote message arrives (a frozen feed would otherwise never re-render).
 */

import { useEffect, useState } from "react";
import { freshSpot, quoteIsStale, useTradingStore } from "../store/tradingStore";
import { useStrategyStore } from "../store/strategyStore";

function fmtEt(ms: number): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(ms));
}

export function PriceReadout({ compact = false }: { compact?: boolean }) {
  const quote = useTradingStore((s) => s.quote);
  const chainSpot = useStrategyStore((s) => s.chain?.spot ?? 0);
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 15_000);
    return () => window.clearInterval(id);
  }, []);

  if (!quote && chainSpot <= 0) {
    return <span className="text-bb-muted">{compact ? "…" : "no quote"}</span>;
  }

  const stale = quoteIsStale(quote);
  const spot = freshSpot(quote, chainSpot);

  return (
    <span data-numeric className={"text-bb-amber" + (compact ? " min-w-0 truncate text-[13px]" : "")}>
      {spot > 0 ? spot.toFixed(2) : "—"}
      {!compact && quote && (
        <span
          className={"ml-3 " + (stale ? "text-bb-orange" : "text-bb-muted")}
          title={
            stale
              ? `Quote stale — book last updated ${fmtEt(quote.ts)} ET; price shown from latest trades`
              : "Live top-of-book"
          }
        >
          {quote.bid.toFixed(2)} × {quote.ask.toFixed(2)}
          {stale ? ` · Q ${fmtEt(quote.ts)}` : ""}
        </span>
      )}
    </span>
  );
}
