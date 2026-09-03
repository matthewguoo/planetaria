/**
 * Controls under the chart on a phone: timeframe chips, extended hours,
 * an indicators popover (VWAP/EMA/BB; HEAT/SIM/THETA in options mode) and
 * FIT. Zoom is the pinch on the canvas; the +/− stay for one-thumb use.
 * Every target is 40px+ tall; nothing overlays the candles.
 */

import { useState } from "react";
import { useCapabilities } from "../../lib/capabilities";
import { TIMEFRAMES, useTradingStore, type IndicatorToggles, type Timeframe } from "../../store/tradingStore";

const INDICATORS: { key: keyof IndicatorToggles; label: string; options: boolean }[] = [
  { key: "vwap", label: "VWAP", options: false },
  { key: "ema", label: "EMA 9/21", options: false },
  { key: "bb", label: "BOLLINGER", options: false },
  { key: "sma", label: "SMA 20/50/200", options: false },
  { key: "rsi", label: "RSI 14", options: false },
  { key: "macd", label: "MACD", options: false },
  { key: "heat", label: "P/L HEAT", options: true },
  { key: "sim", label: "SIM", options: true },
  { key: "theta", label: "THETA CONE", options: true },
];

export function MobileChartBar({
  onZoom,
  onFit,
}: {
  onZoom: (dir: 1 | -1) => void;
  onFit: () => void;
}) {
  const tf = useTradingStore((s) => s.tf);
  const setTf = useTradingStore((s) => s.setTf);
  const showEth = useTradingStore((s) => s.showEth);
  const toggleShowEth = useTradingStore((s) => s.toggleShowEth);
  const indicators = useTradingStore((s) => s.indicators);
  const toggleIndicator = useTradingStore((s) => s.toggleIndicator);
  const optionsMode = useTradingStore((s) => s.assetMode) === "options";
  const caps = useCapabilities();
  const [open, setOpen] = useState(false);

  const visible = INDICATORS.filter(
    (i) => !i.options || (optionsMode && (i.key !== "theta" || caps.spreadsAllowed)),
  );
  const activeCount = visible.filter((i) => indicators[i.key]).length;
  const chip = (on: boolean) =>
    "h-10 min-w-[44px] px-3 text-[13px] tracking-wider " +
    (on ? "bg-bb-amber font-semibold text-black" : "text-bb-muted active:text-bb-amber");

  return (
    <div className="relative flex h-11 shrink-0 items-center gap-1 border-b border-bb-border bg-bb-panel px-1">
      <div className="chip-rail min-w-0 flex-1">
        {TIMEFRAMES.map((option) => (
          <button key={option} onClick={() => setTf(option as Timeframe)} className={chip(tf === option)}>
            {option.toUpperCase()}
          </button>
        ))}
        <button onClick={toggleShowEth} className={chip(showEth)} title="Extended hours bars">
          ETH
        </button>
        <button
          onClick={() => setOpen(!open)}
          className={chip(open) + (activeCount ? " text-bb-amber" : "")}
          aria-label="Indicators"
        >
          ⋯{activeCount ? ` ${activeCount}` : ""}
        </button>
      </div>
      <span className="flex shrink-0 gap-px">
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => onZoom(-1)} aria-label="Zoom in">
          +
        </button>
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => onZoom(1)} aria-label="Zoom out">
          −
        </button>
        <button className="h-10 border border-bb-border px-2 text-[11px] tracking-wider text-bb-muted active:bg-bb-amber active:text-black" onClick={onFit} aria-label="Fit chart">
          FIT
        </button>
      </span>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute left-1 top-full z-40 mt-px flex w-56 flex-col border border-bb-border bg-bb-panel shadow-lg">
            <div className="px-3 py-1.5 text-[10px] tracking-widest text-bb-muted">INDICATORS</div>
            {visible.map((i) => (
              <button
                key={i.key}
                onClick={() => toggleIndicator(i.key)}
                className={
                  "flex h-11 items-center justify-between px-3 text-[13px] " +
                  (indicators[i.key] ? "text-bb-amber" : "text-bb-muted")
                }
              >
                {i.label}
                <span>{indicators[i.key] ? "●" : "○"}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
