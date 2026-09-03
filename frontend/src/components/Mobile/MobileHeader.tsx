/**
 * Phone header — the brokerage-app top strip: symbol (tap → full-screen
 * picker), the price big, the session change beside it in green/red, the
 * session phase, the LIVE/PAPER badge and a feed dot. Nothing else; the
 * chart bar below carries the controls.
 */

import { useEffect, useState } from "react";
import { sharedBars } from "../../lib/chartShared";
import { dayChange } from "../../lib/dayChange";
import { ModeBadge } from "../ModeBadge";
import { sessionPhase } from "../PriceReadout";
import { SymbolSearch } from "../SymbolSearch";
import { useStrategyStore } from "../../store/strategyStore";
import { freshSpot, quoteIsStale, useTradingStore } from "../../store/tradingStore";

const PHASE_CLS: Record<ReturnType<typeof sessionPhase>, string> = {
  RTH: "text-bb-profit",
  PRE: "text-bb-neutral",
  AH: "text-bb-neutral",
  "O/N": "text-bb-neutral",
  CLOSED: "text-bb-muted",
};

function feedDot(status: { connection: string; configured: boolean; stream_age_s: number | null }) {
  if (status.connection !== "open") return { cls: "bg-bb-loss", label: "feed disconnected" };
  if (!status.configured) return { cls: "bg-bb-loss", label: "no keys" };
  if (status.stream_age_s !== null && status.stream_age_s > 30)
    return { cls: "bg-bb-orange", label: `stale ${Math.floor(status.stream_age_s)}s` };
  return { cls: "bg-bb-profit", label: "streaming" };
}

export function MobileHeader() {
  const quote = useTradingStore((s) => s.quote);
  const status = useTradingStore((s) => s.status);
  const chainSpot = useStrategyStore((s) => s.chain?.spot ?? 0);
  // Bars arrive outside React; a slow tick keeps the change current when
  // quotes are quiet (weekend, overnight).
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 5_000);
    return () => window.clearInterval(id);
  }, []);

  const bars = sharedBars.current;
  const lastBar = bars.n ? bars.c[bars.n - 1] : 0;
  const spot = freshSpot(quote, chainSpot || lastBar);
  const change = dayChange(bars, spot);
  const stale = quoteIsStale(quote);
  const phase = sessionPhase();
  const dot = feedDot(status);

  return (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b border-bb-border bg-bb-panel pl-1 pr-3 pt-[env(safe-area-inset-top)]">
      <SymbolSearch variant="sheet" />
      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        <span data-numeric className={"text-[20px] font-semibold " + (stale ? "text-bb-orange" : "text-white")}>
          {spot > 0 ? spot.toFixed(2) : "—"}
        </span>
        <span className="flex items-center gap-2 text-[11px]">
          {change ? (
            <span data-numeric className={change.change >= 0 ? "text-bb-profit" : "text-bb-loss"}>
              {change.change >= 0 ? "+" : "−"}
              {Math.abs(change.change).toFixed(2)} ({change.pct >= 0 ? "+" : "−"}
              {Math.abs(change.pct * 100).toFixed(2)}%)
            </span>
          ) : (
            <span className="text-bb-muted">—</span>
          )}
          <span className={PHASE_CLS[phase]}>{phase}</span>
          {stale && <span className="text-bb-orange">quote stale</span>}
        </span>
      </div>
      <span className={`h-2.5 w-2.5 rounded-full ${dot.cls}`} title={dot.label} aria-label={dot.label} />
      <ModeBadge />
    </header>
  );
}
