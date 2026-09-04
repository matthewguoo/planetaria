/**
 * Phone shell (< 640px or a touch device on its short side). Bottom tabs
 * HOME · TRADE · ACCOUNT · MORE. HOME is the account: equity curve, the
 * book as one-line rows, working orders — a row opens its sheet with every
 * detail and action. TRADE is the chart with the ticket; it stays mounted
 * (feed subscriptions survive) and a position sheet's CHART lands there in
 * position view, whose ✕ returns to HOME.
 *
 * No desktop file grows for this — the phone lives under components/Mobile/
 * and reuses the stores, the designer and the order payload unchanged.
 */

import { useRef, useState } from "react";
import type { Plan, UntrackedPosition } from "../../lib/api";
import { useDesigner } from "../../lib/useDesigner";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { useEquityTicketStore } from "../../store/equityTicketStore";
import { useStrategyStore } from "../../store/strategyStore";
import { useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import { EnforcementBanner } from "../EnforcementBanner";
import { CandlePane } from "../Chart/CandlePane";
import { ChainPanel } from "../Chart/ChainPanel";
import { LegRail } from "../Chart/LegRail";
import { EquityTicket } from "../Panels/EquityTicket";
import { MobileAccount } from "./MobileAccount";
import { MobileChartBar } from "./MobileChartBar";
import { MobileHeader } from "./MobileHeader";
import { MobileHome } from "./MobileHome";
import { MobileMore } from "./MobileMore";
import { MobileOpenOrders } from "./MobileOrders";
import { MobileOptionsTicket } from "./MobileOptionsTicket";
import { MobilePositions } from "./MobilePositions";
import { AccountStrip } from "./MobileUi";
import { Sheet } from "./Sheet";

type Tab = "home" | "trade" | "account" | "more";
type Dock = "positions" | "orders" | "chain";

/** Read-only position-view banner: which plan the chart is inspecting. */
function MobilePositionBanner({ onBack }: { onBack: () => void }) {
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
        "flex h-10 items-center gap-2 border-b px-3 text-[12px] " +
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
        <span data-numeric className={plan.realized_pnl >= 0 ? "text-bb-profit" : "text-bb-loss"}>
          {plan.realized_pnl >= 0 ? "+" : "−"}${Math.abs(plan.realized_pnl).toFixed(0)}
        </span>
      )}
      {!closed && plan.asset_class !== "equity" && (
        <span className="flex gap-px">
          {(["entry", "live"] as const).map((m) => (
            <button
              key={m}
              className={"h-8 px-2 text-[11px] " + (pnlMode === m ? "bg-bb-amber text-black" : "text-bb-muted")}
              onClick={() => setPnlMode(m)}
            >
              {m.toUpperCase()}
            </button>
          ))}
        </span>
      )}
      <button className="ml-auto h-10 w-10 text-[16px] text-bb-muted" onClick={() => { closePositionView(); onBack(); }} aria-label="Back">
        ✕
      </button>
    </div>
  );
}

export function MobileApp() {
  const designer = useDesigner();
  const assetMode = useTradingStore((s) => s.assetMode);
  const setAssetMode = useTradingStore((s) => s.setAssetMode);
  const positions = useAccountStore((s) => s.positions);
  const untracked = useAccountStore((s) => s.untracked);
  const modified = useStrategyStore((s) => s.modified);
  const prefillFromPlan = useStrategyStore((s) => s.prefillFromPlan);
  const viewPosition = useUiStore((s) => s.viewPosition);
  const setSymbol = useTradingStore((s) => s.setSymbol);
  const equityTicket = useEquityTicketStore();
  const { live } = useTradingMode();
  const [tab, setTab] = useState<Tab>("home");
  const [returnTab, setReturnTab] = useState<Tab>("home");
  const [dock, setDock] = useState<Dock>("positions");
  const [dockOpen, setDockOpen] = useState(true);
  const [ticket, setTicket] = useState(false);
  const chartWrapRef = useRef<HTMLDivElement>(null);
  const optionsMode = assetMode === "options";

  // Zoom/fit drive the SAME wheel/dblclick paths the desktop uses, via
  // synthetic events at the chart centre — no chart-code fork for mobile.
  const chartCanvas = () => chartWrapRef.current?.querySelector("canvas") ?? null;
  const zoom = (dir: 1 | -1) => {
    const canvas = chartCanvas();
    if (!canvas) return;
    const r = canvas.getBoundingClientRect();
    canvas.dispatchEvent(new WheelEvent("wheel", {
      deltaY: dir * 240, clientX: r.left + r.width * 0.55, clientY: r.top + r.height * 0.5, bubbles: true, cancelable: true,
    }));
  };
  const fit = () => {
    const canvas = chartCanvas();
    if (!canvas) return;
    const r = canvas.getBoundingClientRect();
    canvas.dispatchEvent(new MouseEvent("dblclick", {
      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, bubbles: true, cancelable: true,
    }));
  };

  /** A position sheet's CHART: the TRADE tab in position view; ✕ returns here. */
  const chartFor = (plan: Plan) => {
    setSymbol(plan.underlying);
    setAssetMode(plan.asset_class === "equity" ? "equity" : "options");
    viewPosition(plan.id);
    setReturnTab(tab);
    setTab("trade");
  };
  /** CHART for an untracked broker position: its underlying, options or shares mode, no plan to view. */
  const chartForUntracked = (pos: UntrackedPosition) => {
    setSymbol(pos.occ ? pos.occ.underlying : pos.symbol);
    setAssetMode(pos.occ ? "options" : "equity");
    setReturnTab(tab);
    setTab("trade");
  };
  /** ADD from a position sheet: the ticket staged on the plan's structure. */
  const addTo = (plan: Plan) => {
    setSymbol(plan.underlying);
    if (plan.asset_class === "equity") {
      setAssetMode("equity");
      equityTicket.setSide(plan.legs[0].side > 0 ? 1 : -1);
      equityTicket.setSharesOverride(plan.filled_qty || plan.qty);
    } else {
      setAssetMode("options");
      prefillFromPlan(plan);
    }
    setReturnTab(tab);
    setTab("trade");
    setTicket(true);
  };

  const openCount = positions.length + untracked.length;
  const dockTabs: { id: Dock; label: string }[] = [
    { id: "positions", label: `BOOK${openCount ? ` ${openCount}` : ""}` },
    { id: "orders", label: "ORDERS" },
    ...(optionsMode ? [{ id: "chain" as Dock, label: "CHAIN" }] : []),
  ];
  const activeDock = dockTabs.some((d) => d.id === dock) ? dock : "positions";

  const navBtn = (id: Tab, label: string, badge?: number) => (
    <button
      onClick={() => setTab(id)}
      className={
        "flex h-full flex-1 flex-col items-center justify-center gap-0.5 text-[11px] tracking-widest " +
        (tab === id ? "text-bb-amber" : "text-bb-muted active:text-bb-amber")
      }
      aria-current={tab === id ? "page" : undefined}
    >
      <span>{label}</span>
      {badge ? <span data-numeric className="text-[9px] text-bb-muted">{badge}</span> : <span className="h-3" />}
    </button>
  );

  return (
    <div className="flex h-[100dvh] flex-col bg-bb-black">
      <MobileHeader />
      <EnforcementBanner />

      {tab === "home" && <MobileHome onChart={chartFor} onChartUntracked={chartForUntracked} onAdd={addTo} onAccount={() => setTab("account")} />}

      {/* TRADE: chart + dock. Kept mounted (hidden) under the other tabs. */}
      <div className={"flex min-h-0 flex-1 flex-col" + (tab === "trade" ? "" : " hidden")}>
        <MobilePositionBanner onBack={() => setTab(returnTab)} />
        <MobileChartBar onZoom={zoom} onFit={fit} />
        <div ref={chartWrapRef} className="relative min-h-0 flex-1">
          <CandlePane designer={designer} hudVariant="readout" />
          {optionsMode && <LegRail designer={designer} />}
        </div>
        <AccountStrip onClick={() => setTab("home")} />
        <div className="flex shrink-0 flex-col border-t border-bb-border bg-bb-panel">
          <div className="flex h-10 items-center">
            {dockTabs.map(({ id, label }) => (
              <button
                key={id}
                onClick={() => { setDock(id); setDockOpen(activeDock === id ? !dockOpen : true); }}
                className={
                  "h-full flex-1 text-[11px] tracking-widest " +
                  (dockOpen && activeDock === id ? "border-b-2 border-bb-amber font-semibold text-bb-amber" : "text-bb-muted")
                }
              >
                {label}
              </button>
            ))}
            <button className="h-full px-4 text-[12px] text-bb-muted" onClick={() => setDockOpen(!dockOpen)} aria-label={dockOpen ? "Collapse" : "Expand"}>
              {dockOpen ? "▾" : "▴"}
            </button>
          </div>
          {dockOpen && (
            <div className="flex h-[26dvh] min-h-36 flex-col border-t border-bb-border/60 bg-black">
              {activeDock === "positions" && <MobilePositions />}
              {activeDock === "orders" && <MobileOpenOrders />}
              {activeDock === "chain" && <div className="min-h-0 flex-1"><ChainPanel /></div>}
            </div>
          )}
        </div>
        <div className="shrink-0 border-t border-bb-border bg-bb-panel p-2">
          <button
            onClick={() => setTicket(true)}
            className={
              "h-12 w-full text-[13px] font-semibold tracking-widest text-black " +
              (live ? "bg-bb-loss active:bg-bb-orange" : "bg-bb-amber active:bg-bb-orange")
            }
          >
            TRADE · {assetMode === "equity" ? "SHARES" : modified ? "CUSTOM OPTIONS" : "OPTIONS"}
            {live ? " · LIVE" : ""}
          </button>
        </div>
      </div>

      {tab === "account" && (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
          <MobileAccount />
        </div>
      )}
      {tab === "more" && (
        <div className="flex min-h-0 flex-1 flex-col">
          <MobileMore />
        </div>
      )}

      <nav className="flex h-14 shrink-0 items-stretch border-t border-bb-border bg-bb-panel pb-[env(safe-area-inset-bottom)]">
        {navBtn("home", "HOME", openCount)}
        {navBtn("trade", "TRADE")}
        {navBtn("account", "ACCOUNT")}
        {navBtn("more", "MORE")}
      </nav>

      {ticket && (
        <Sheet
          title="TRADE"
          onClose={() => setTicket(false)}
          tall
          right={
            <span className="flex gap-px pr-1">
              {(["options", "equity"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setAssetMode(m)}
                  className={
                    "h-9 px-3 text-[11px] tracking-widest " +
                    (assetMode === m ? "bg-bb-amber font-semibold text-black" : "border border-bb-border text-bb-muted")
                  }
                >
                  {m === "options" ? "OPTIONS" : "SHARES"}
                </button>
              ))}
            </span>
          }
        >
          {assetMode === "equity" ? (
            <div className="flex min-h-full flex-col"><EquityTicket /></div>
          ) : (
            <MobileOptionsTicket designer={designer} />
          )}
        </Sheet>
      )}
    </div>
  );
}
