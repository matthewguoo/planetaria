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
import { EnforcementBanner } from "../EnforcementBanner";
import { PriceReadout } from "../PriceReadout";
import { useUiStore } from "../../store/uiStore";
import { AccountPage } from "../Account/AccountPage";
import { CandlePane } from "../Chart/CandlePane";
import { OrderPanel } from "../Panels/OrderPanel";
import { SizingPanel } from "../Panels/SizingPanel";
import { StrategyPanel } from "../Panels/StrategyPanel";
import { SymbolSearch } from "../SymbolSearch";
import { SystemMenu } from "../System/SystemMenu";
import { MobileDataTabs } from "./MobileDataTabs";
import { Sheet } from "./Sheet";

type SheetTab = null | "trade" | "account";

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
  const [systemOpen, setSystemOpen] = useState(false);
  useEffect(() => onAudioModeChange(setAudio), []);
  const pill = statusColor(status, symbol);

  return (
    <header className="flex h-9 shrink-0 items-center gap-2 border-b border-bb-border bg-bb-panel px-2">
      <SymbolSearch />
      <PriceReadout compact />
      <span className="ml-auto flex items-center gap-2">
        <button
          className={"text-[11px] " + (audio === "off" ? "text-bb-muted" : "text-bb-amber")}
          onClick={() => cycleAudioMode()}
          aria-label="Cycle audio mode"
        >
          {audio === "off" ? "🔇" : audio === "fx" ? "🔊" : "🗣"}
        </button>
        <button
          className="text-[13px] text-bb-muted active:text-bb-amber"
          onClick={() => setSystemOpen(true)}
          aria-label="System menu"
        >
          ⚙
        </button>
        <span className={`h-2 w-2 rounded-full ${pill.cls}`} title={pill.label} />
      </span>
      {systemOpen && <SystemMenu onClose={() => setSystemOpen(false)} />}
    </header>
  );
}

/** Read-only position-view banner (desktop's lives in ChartControls).
 * Resolves CLOSED trades from the history snapshot too — the ✕ must always
 * be reachable from a position view. */
function MobilePositionBanner() {
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const viewedHistorical = useUiStore((s) => s.viewedHistorical);
  const pnlMode = useUiStore((s) => s.pnlMode);
  const setPnlMode = useUiStore((s) => s.setPnlMode);
  const closePositionView = useUiStore((s) => s.closePositionView);
  const positions = useAccountStore((s) => s.positions);
  const plan = viewingPlanId
    ? positions.find((p) => p.id === viewingPlanId) ??
      (viewedHistorical?.id === viewingPlanId ? viewedHistorical : null)
    : null;
  if (!plan) return null;
  const closed = ["closed", "cancelled", "rejected"].includes(plan.status);
  return (
    <div
      className={
        "flex items-center gap-2 border-b px-2 py-1 text-[10px] " +
        (closed ? "border-bb-border bg-bb-hover/40" : "border-bb-amber/60 bg-bb-amber/10")
      }
    >
      <span className={"tracking-widest " + (closed ? "text-bb-muted" : "text-bb-amber")}>
        {closed ? `CLOSED · ${(plan.exit_reason ?? plan.status).toUpperCase()}` : "POSITION"}
      </span>
      <span className="truncate text-white">
        {plan.underlying} ×{plan.filled_qty || plan.qty}
      </span>
      {closed && plan.realized_pnl != null && (
        <span
          data-numeric
          className={plan.realized_pnl >= 0 ? "text-bb-profit" : "text-bb-loss"}
        >
          {plan.realized_pnl >= 0 ? "+" : "−"}${Math.abs(plan.realized_pnl).toFixed(0)}
        </span>
      )}
      {!closed && (
        <>
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
        </>
      )}
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

  return (
    <div className="flex h-[100dvh] flex-col bg-bb-black">
      <MobileHeader />
      <EnforcementBanner />
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

      {/* The chart is the hero: chips-only HUD, all dense data lives in
          the exchange-style tab strip below. */}
      <div ref={chartWrapRef} className="relative min-h-0 flex-1">
        <CandlePane designer={designer} hudVariant="chips" />
      </div>

      <MobileDataTabs designer={designer} />

      <nav className="flex h-12 shrink-0 items-stretch gap-px border-t border-bb-border bg-bb-panel pb-[env(safe-area-inset-bottom)]">
        <button
          onClick={() => setSheet(sheet === "trade" ? null : "trade")}
          className={
            "flex-[2] text-[12px] font-semibold tracking-widest " +
            (sheet === "trade" ? "bg-bb-hover text-bb-amber" : "bg-bb-amber text-black")
          }
        >
          {modified ? "TRADE · CUSTOM" : "TRADE"}
        </button>
        <button
          onClick={() => setSheet(sheet === "account" ? null : "account")}
          className={
            "flex-1 text-[11px] tracking-widest " +
            (sheet === "account" ? "bg-bb-hover text-bb-amber" : "text-bb-muted")
          }
        >
          ACCOUNT{positions.length ? ` · ${positions.length}` : ""}
        </button>
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
      {sheet === "account" && (
        <Sheet title="ACCOUNT" onClose={() => setSheet(null)} tall>
          <AccountPage />
        </Sheet>
      )}
    </div>
  );
}
