/**
 * Phone shell (< 640px or a touch device on its short side). The pattern
 * is the one every phone brokerage converged on: a bottom tab bar
 * (CHART · POSITIONS · ACCOUNT · MORE), the chart as the home screen with
 * the live book docked under it, and one big TRADE button that opens the
 * ticket as a sheet over the still-live chart. The chart itself stays
 * mounted across tabs (feed subscriptions survive), it is only hidden.
 *
 * No desktop file grows for this — the phone lives under components/Mobile/
 * and reuses the stores, the designer and the order payload unchanged.
 */

import { useRef, useState } from "react";
import { useDesigner } from "../../lib/useDesigner";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
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
import { MobileMore } from "./MobileMore";
import { MobileOpenOrders } from "./MobileOrders";
import { MobileOptionsTicket } from "./MobileOptionsTicket";
import { MobilePositions } from "./MobilePositions";
import { Sheet } from "./Sheet";

type Tab = "chart" | "positions" | "account" | "more";
type Dock = "positions" | "orders" | "chain";

/** Read-only position-view banner: which plan the chart is inspecting. */
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
      <button className="ml-auto h-10 w-10 text-[16px] text-bb-muted" onClick={closePositionView} aria-label="Back to designer">
        ✕
      </button>
    </div>
  );
}

/** One-line book summary between the chart and the dock. */
function AccountStrip() {
  const account = useAccountStore((s) => s.account);
  const positions = useAccountStore((s) => s.positions);
  const unrealized = positions.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);
  const day = (account?.day_realized_pnl ?? 0) + unrealized;
  const cls = day >= 0 ? "text-bb-profit" : "text-bb-loss";
  return (
    <div className="flex h-8 shrink-0 items-center gap-4 border-b border-bb-border bg-bb-panel px-3 text-[11px]">
      <span className="text-bb-muted">
        EQUITY <span data-numeric className="text-white">{account ? `$${Math.round(account.equity).toLocaleString()}` : "—"}</span>
      </span>
      <span className="text-bb-muted">
        TODAY{" "}
        <span data-numeric className={cls}>
          {account ? `${day >= 0 ? "+" : "−"}$${Math.abs(day).toFixed(0)}` : "—"}
          {account && account.equity > 0 ? ` (${day >= 0 ? "+" : "−"}${Math.abs((day / account.equity) * 100).toFixed(2)}%)` : ""}
        </span>
      </span>
      <span className="ml-auto text-bb-muted">
        CASH <span data-numeric className="text-white">{account ? `$${Math.round(account.cash).toLocaleString()}` : "—"}</span>
      </span>
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
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const { live } = useTradingMode();
  const [tab, setTab] = useState<Tab>("chart");
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

  const openCount = positions.length + untracked.length;
  const dockTabs: { id: Dock; label: string }[] = [
    { id: "positions", label: `POSITIONS${openCount ? ` ${openCount}` : ""}` },
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

      {/* HOME: chart + dock. Kept mounted (hidden) under the other tabs. */}
      <div className={"flex min-h-0 flex-1 flex-col" + (tab === "chart" ? "" : " hidden")}>
        <MobilePositionBanner />
        <MobileChartBar onZoom={zoom} onFit={fit} />
        <div ref={chartWrapRef} className="relative min-h-0 flex-1">
          <CandlePane designer={designer} hudVariant="readout" />
          {optionsMode && <LegRail designer={designer} />}
        </div>
        <AccountStrip />
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
              {activeDock === "positions" && <MobilePositions compact />}
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

      {tab === "positions" && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex h-10 items-center px-3 text-[12px] tracking-widest text-bb-amber">POSITIONS</div>
          <MobilePositions />
        </div>
      )}
      {tab === "account" && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex h-10 items-center px-3 text-[12px] tracking-widest text-bb-amber">ACCOUNT</div>
          <MobileAccount />
        </div>
      )}
      {tab === "more" && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex h-10 items-center px-3 text-[12px] tracking-widest text-bb-amber">SYSTEM</div>
          <MobileMore />
        </div>
      )}

      <nav className="flex h-14 shrink-0 items-stretch border-t border-bb-border bg-bb-panel pb-[env(safe-area-inset-bottom)]">
        {navBtn("chart", "CHART")}
        {navBtn("positions", "POSITIONS", openCount)}
        {navBtn("account", "ACCOUNT")}
        {navBtn("more", "MORE")}
      </nav>

      {ticket && (
        <Sheet
          title={viewingPlanId ? "TRADE TICKET · position view exits on any edit" : "TRADE TICKET"}
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
