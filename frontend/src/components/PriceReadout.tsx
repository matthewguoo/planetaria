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
import { fmtTimeET as fmtEt } from "./Chart/scales";

/** US equity session phase in ET. Extended hours are 04:00–09:30 (PRE) and
 * 16:00–20:00 (AH). 20:00–04:00 is the OVERNIGHT session (Blue Ocean ATS,
 * Sun 20:00 → Fri 04:00) — the app polls its last trade so the chart keeps
 * moving. Only Sat and most of Sunday are truly CLOSED. */
export function sessionPhase(nowMs: number = Date.now()): "PRE" | "RTH" | "AH" | "O/N" | "CLOSED" {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    })
      .formatToParts(new Date(nowMs))
      .map((p) => [p.type, p.value]),
  );
  const minute = Number(parts.hour === "24" ? 0 : parts.hour) * 60 + Number(parts.minute);
  const day = parts.weekday;
  if (day === "Sat") return "CLOSED";
  if (day === "Sun") return minute >= 20 * 60 ? "O/N" : "CLOSED";
  if (minute < 4 * 60) return "O/N"; // Mon-Fri tail of the overnight session
  if (day !== "Fri" && minute >= 20 * 60) return "O/N";
  if (minute >= 4 * 60 && minute < 9 * 60 + 30) return "PRE";
  if (minute >= 9 * 60 + 30 && minute < 16 * 60) return "RTH";
  if (minute >= 16 * 60 && minute < 20 * 60) return "AH";
  return "CLOSED"; // Fri 20:00+
}

const PHASE_STYLE: Record<ReturnType<typeof sessionPhase>, string> = {
  RTH: "text-bb-profit",
  PRE: "text-bb-neutral",
  AH: "text-bb-neutral",
  "O/N": "text-bb-neutral",
  CLOSED: "text-bb-muted",
};

const PHASE_TITLE: Record<ReturnType<typeof sessionPhase>, string> = {
  RTH: "Regular trading hours (09:30–16:00 ET)",
  PRE: "Pre-market (04:00–09:30 ET) — thin extended-hours prints",
  AH: "After hours (16:00–20:00 ET) — thin extended-hours prints",
  "O/N": "Overnight session (Blue Ocean ATS, Sun 20:00 → Fri 04:00 ET) — last trade polled ~10s; thin institutional prints",
  CLOSED: "Market closed (Fri 20:00 ET → Sun 20:00 ET) — no prints anywhere; last candle is final",
};

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
  const phase = sessionPhase();

  return (
    <span data-numeric className={"text-bb-amber" + (compact ? " min-w-0 truncate text-[13px]" : "")}>
      {spot > 0 ? spot.toFixed(2) : "—"}
      {!compact && quote && quote.bid !== quote.ask && (
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
      {!compact && quote && quote.bid === quote.ask && (
        <span className="ml-3 text-bb-muted" title="Overnight trade print (no book) — bid/ask resume at 04:00 ET">
          LAST {fmtEt(quote.ts)} ET
        </span>
      )}
      <span className={"ml-2 text-[10px] " + PHASE_STYLE[phase]} title={PHASE_TITLE[phase]}>
        {phase}
      </span>
    </span>
  );
}
