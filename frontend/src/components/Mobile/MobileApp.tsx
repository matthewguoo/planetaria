/**
 * Phone layout (< 640px): the candle chart IS the screen. A slim header,
 * a timeframe/overlay strip, zoom buttons, and a bottom nav whose sheets
 * reuse the desktop panels unchanged (Strategy/Sizing/Order, chain,
 * positions drawer, account page). No desktop file grows for this — the
 * mobile shell lives entirely under components/Mobile/.
 */

import { useEffect, useRef, useState } from "react";
import { cycleAudioMode, getAudioMode, onAudioModeChange } from "../../lib/audio";
import { useDesigner } from "../../lib/useDesigner";
import { useAccountStore } from "../../store/accountStore";
import { useStrategyStore } from "../../store/strategyStore";
import { TIMEFRAMES, useTradingStore, type Timeframe } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import { AccountPage } from "../Account/AccountPage";
import { CandlePane } from "../Chart/CandlePane";
import { ChainPanel } from "../Chart/ChainPanel";
import { OrderPanel } from "../Panels/OrderPanel";
import { SizingPanel } from "../Panels/SizingPanel";
import { StrategyPanel } from "../Panels/StrategyPanel";
import { PositionsDrawer } from "../Positions/PositionsDrawer";
import { SymbolSearch } from "../SymbolSearch";
import { Sheet } from "./Sheet";

type SheetTab = null | "trade" | "chain" | "positions" | "account";

function statusColor(status: {
  connection: string;
  demo: boolean;
  configured: boolean;
  sources: Record<string, string>;
}, symbol: string): { cls: string; label: string } {
  if (status.connection !== "open") return { cls: "bg-bb-loss", label: "feed disconnected" };
  if (status.demo) {
    return status.sources[symbol] === "public"
      ? { cls: "bg-bb-amber", label: "public data (real prices, keyless)" }
      : { cls: "bg-bb-muted", label: "synthetic demo data" };
  }
  if (!status.configured) return { cls: "bg-bb-loss", label: "no keys" };
  return { cls: "bg-bb-profit", label: "live" };
}

function MobileHeader() {
  const symbol = useTradingStore((s) => s.symbol);
  const quote = useTradingStore((s) => s.quote);
  const status = useTradingStore((s) => s.status);
  const [audio, setAudio] = useState(getAudioMode());
  useEffect(() => onAudioModeChange(setAudio), []);
  const pill = statusColor(status, symbol);

  return (
    <header className="flex h-9 shrink-0 items-center gap-2 border-b border-bb-border bg-bb-panel px-2">
      <SymbolSearch />
      {quote ? (
        <span data-numeric className="min-w-0 truncate text-[13px] text-bb-amber">
          {quote.mid ? quote.mid.toFixed(2) : "—"}
        </span>
      ) : (
        <span className="text-[11px] text-bb-muted">…</span>
      )}
      <span className="ml-auto flex items-center gap-2">
        <button
          className={"text-[11px] " + (audio === "off" ? "text-bb-muted" : "text-bb-amber")}
          onClick={() => cycleAudioMode()}
          aria-label="Cycle audio mode"
        >
          {audio === "off" ? "🔇" : audio === "fx" ? "🔊" : "🗣"}
        </button>
        <span className={`h-2 w-2 rounded-full ${pill.cls}`} title={pill.label} />
      </span>
    </header>
  );
}

/** Read-only position-view banner (desktop's lives in ChartControls). */
function MobilePositionBanner() {
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const pnlMode = useUiStore((s) => s.pnlMode);
  const setPnlMode = useUiStore((s) => s.setPnlMode);
  const closePositionView = useUiStore((s) => s.closePositionView);
  const positions = useAccountStore((s) => s.positions);
  const plan = viewingPlanId ? positions.find((p) => p.id === viewingPlanId) ?? null : null;
  if (!plan) return null;
  return (
    <div className="flex items-center gap-2 border-b border-bb-amber/60 bg-bb-amber/10 px-2 py-1 text-[10px]">
      <span className="tracking-widest text-bb-amber">POSITION</span>
      <span className="truncate text-white">
        {plan.underlying} ×{plan.filled_qty || plan.qty}
      </span>
      <button
        className={"px-1 " + (pnlMode === "entry" ? "bg-bb-amber text-black" : "text-bb-muted")}
        onClick={() => setPnlMode("entry")}
      >
        ENTRY
      </button>
      <button
        className={"px-1 " + (pnlMode === "live" ? "bg-bb-amber text-black" : "text-bb-muted")}
        onClick={() => setPnlMode("live")}
      >
        LIVE
      </button>
      <button className="ml-auto px-1 text-bb-muted" onClick={closePositionView}>
        ✕
      </button>
    </div>
  );
}

export function MobileApp() {
  const designer = useDesigner();
  const tf = useTradingStore((s) => s.tf);
  const setTf = useTradingStore((s) => s.setTf);
  const positions = useAccountStore((s) => s.positions);
  const modified = useStrategyStore((s) => s.modified);
  const [sheet, setSheet] = useState<SheetTab>(null);
  const chartWrapRef = useRef<HTMLDivElement>(null);

  // Zoom buttons drive the SAME wheel/dblclick paths the desktop uses, via
  // synthetic events at the chart centre — no chart-code fork for mobile.
  const chartCanvas = () => chartWrapRef.current?.querySelector("canvas") ?? null;
  const zoom = (dir: 1 | -1) => {
    const canvas = chartCanvas();
    if (!canvas) return;
    const r = canvas.getBoundingClientRect();
    canvas.dispatchEvent(
      new WheelEvent("wheel", {
        deltaY: dir * 240,
        clientX: r.left + r.width * 0.55,
        clientY: r.top + r.height * 0.5,
        bubbles: true,
        cancelable: true,
      }),
    );
  };
  const resetView = () => {
    const canvas = chartCanvas();
    if (!canvas) return;
    const r = canvas.getBoundingClientRect();
    canvas.dispatchEvent(
      new MouseEvent("dblclick", {
        clientX: r.left + r.width / 2,
        clientY: r.top + r.height / 2,
        bubbles: true,
        cancelable: true,
      }),
    );
  };

  const NAV: { tab: Exclude<SheetTab, null>; label: string; badge?: string }[] = [
    { tab: "trade", label: modified ? "TRADE·C" : "TRADE" },
    { tab: "chain", label: "CHAIN" },
    { tab: "positions", label: `POS${positions.length ? ` ${positions.length}` : ""}` },
    { tab: "account", label: "ACCT" },
  ];

  return (
    <div className="flex h-[100dvh] flex-col bg-bb-black">
      <MobileHeader />
      <MobilePositionBanner />

      <div className="flex shrink-0 items-center gap-1 border-b border-bb-border bg-bb-panel px-1 py-0.5">
        {TIMEFRAMES.map((option) => (
          <button
            key={option}
            onClick={() => setTf(option as Timeframe)}
            className={
              "px-2 py-0.5 text-[11px] " +
              (tf === option ? "bg-bb-amber font-semibold text-black" : "text-bb-muted")
            }
          >
            {option.toUpperCase()}
          </button>
        ))}
        <span className="ml-auto flex gap-1">
          <button className="border border-bb-border px-2.5 py-0.5 text-[13px] text-bb-muted active:text-bb-amber" onClick={() => zoom(-1)} aria-label="Zoom in">
            +
          </button>
          <button className="border border-bb-border px-2.5 py-0.5 text-[13px] text-bb-muted active:text-bb-amber" onClick={() => zoom(1)} aria-label="Zoom out">
            −
          </button>
          <button className="border border-bb-border px-2 py-0.5 text-[10px] text-bb-muted active:text-bb-amber" onClick={resetView} aria-label="Reset view">
            FIT
          </button>
        </span>
      </div>

      {/* The chart is the screen. */}
      <div ref={chartWrapRef} className="relative min-h-0 flex-1">
        <CandlePane designer={designer} />
      </div>

      <nav className="flex h-11 shrink-0 border-t border-bb-border bg-bb-panel pb-[env(safe-area-inset-bottom)]">
        {NAV.map(({ tab, label }) => (
          <button
            key={tab}
            onClick={() => setSheet(sheet === tab ? null : tab)}
            className={
              "flex-1 text-[11px] tracking-widest " +
              (sheet === tab ? "bg-bb-amber font-semibold text-black" : "text-bb-muted")
            }
          >
            {label}
          </button>
        ))}
      </nav>

      {sheet === "trade" && (
        <Sheet title="TRADE TICKET" onClose={() => setSheet(null)}>
          <div className="flex flex-col gap-px">
            <div className="h-56"><StrategyPanel designer={designer} /></div>
            <div className="h-64"><SizingPanel designer={designer} /></div>
            <div className="h-72"><OrderPanel designer={designer} /></div>
          </div>
        </Sheet>
      )}
      {sheet === "chain" && (
        <Sheet title="OPTIONS CHAIN — B/S adds a leg" onClose={() => setSheet(null)}>
          <div className="h-[60dvh]"><ChainPanel /></div>
        </Sheet>
      )}
      {sheet === "positions" && (
        <Sheet title="POSITIONS" onClose={() => setSheet(null)}>
          <div className="flex h-[60dvh] flex-col"><PositionsDrawer /></div>
        </Sheet>
      )}
      {sheet === "account" && (
        <Sheet title="ACCOUNT" onClose={() => setSheet(null)} tall>
          <AccountPage />
        </Sheet>
      )}
    </div>
  );
}
