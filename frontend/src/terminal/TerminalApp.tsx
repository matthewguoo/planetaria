import { useEffect, useState } from "react";
import { getFeedSettings } from "../lib/api";
import { AccountPage } from "../components/Account/AccountPage";
import { EnforcementBanner } from "../components/EnforcementBanner";
import { TerminalHeader } from "../components/TerminalHeader";
import { CandlePane } from "../components/Chart/CandlePane";
import { ChainPanel } from "../components/Chart/ChainPanel";
import { ChartControls } from "../components/Chart/ChartControls";
import { ChartHud } from "../components/Chart/ChartHud";
import { LegRail } from "../components/Chart/LegRail";
import { sharedBars } from "../lib/chartShared";
import { MobileApp } from "../components/Mobile/MobileApp";
import { EquityTicket } from "../components/Panels/EquityTicket";
import { OrderPanel } from "../components/Panels/OrderPanel";
import { SizingPanel } from "../components/Panels/SizingPanel";
import { StrategyPanel } from "../components/Panels/StrategyPanel";
import { PositionsDrawer } from "../components/Positions/PositionsDrawer";
import StrategiesPage from "../components/Strategies/StrategiesPage";
import { useDesigner } from "../lib/useDesigner";
import { useAccountStore } from "../store/accountStore";
import { useStrategyStore } from "../store/strategyStore";
import { useTradingStore } from "../store/tradingStore";
import { useUiStore } from "../store/uiStore";

// Below this width the dedicated phone layout takes over (chart-first,
// bottom-sheet panels — see components/Mobile/). A touch device whose SHORT
// side is under it is a phone too — landscape phones are ~850px wide and
// were getting the eleven-column desktop grid.
const MIN_WIDTH = 640;

function isPhoneViewport(): boolean {
  const coarse = window.matchMedia?.("(pointer: coarse)").matches ?? false;
  const shortSide = Math.min(window.innerWidth, window.innerHeight);
  return window.innerWidth < MIN_WIDTH || (coarse && shortSide < MIN_WIDTH);
}

function usePhoneViewport(): boolean {
  const [phone, setPhone] = useState(isPhoneViewport);
  useEffect(() => {
    const onResize = () => setPhone(isPhoneViewport());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return phone;
}

/** Background data pumps; cadences come from the feed settings (SYSTEM ⚙
 * menu) with sane defaults until they load. */
function useDataPumps() {
  const symbol = useTradingStore((s) => s.symbol);
  const assetMode = useTradingStore((s) => s.assetMode);
  const loadChain = useStrategyStore((s) => s.loadChain);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const [cadence, setCadence] = useState({ chain: 10_000, account: 30_000, positions: 5_000 });

  useEffect(() => {
    getFeedSettings()
      .then((cfg) =>
        setCadence({
          chain: cfg.chain_refresh_s * 1000,
          account: cfg.account_poll_s * 1000,
          positions: cfg.positions_poll_s * 1000,
        }),
      )
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (assetMode !== "options") return; // no chain pump for the share ticket
    void loadChain(symbol);
    const id = window.setInterval(() => void loadChain(symbol), cadence.chain);
    return () => window.clearInterval(id);
  }, [symbol, loadChain, cadence.chain, assetMode]);

  useEffect(() => {
    void refreshAccount();
    void refreshPositions();
    const acctId = window.setInterval(() => void refreshAccount(), cadence.account);
    const posId = window.setInterval(() => void refreshPositions(), cadence.positions);
    return () => {
      window.clearInterval(acctId);
      window.clearInterval(posId);
    };
  }, [refreshAccount, refreshPositions, cadence.account, cadence.positions]);
}

// ?unlock forces the desktop layout in small panes (QA/testing).
const UNLOCKED = new URLSearchParams(window.location.search).has("unlock");

export default function TerminalApp() {
  const phone = usePhoneViewport();
  useDataPumps();
  const designer = useDesigner();
  const view = useUiStore((s) => s.view);
  const chainOpen = useUiStore((s) => s.chainOpen);
  const viewPosition = useUiStore((s) => s.viewPosition);
  const setSymbol = useTradingStore((s) => s.setSymbol);
  const assetMode = useTradingStore((s) => s.assetMode);
  const setAssetMode = useTradingStore((s) => s.setAssetMode);
  const optionsMode = assetMode === "options";

  if (phone && !UNLOCKED) return <MobileApp />;

  return (
    <div className="flex h-full flex-col gap-px bg-bb-black p-px">
      <TerminalHeader />
      <EnforcementBanner />

      {view === "account" ? (
        <AccountPage
          onViewPlan={(plan) => {
            setSymbol(plan.underlying);
            viewPosition(plan.id);
          }}
        />
      ) : view === "strategies" ? (
        <StrategiesPage />
      ) : (
        <>
          <main className="panel flex min-h-0 flex-1 flex-col">
            <div className="flex items-center gap-2 border-b border-bb-border">
              <div className="flex gap-px px-1 py-0.5">
                {(["options", "equity"] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setAssetMode(m)}
                    className={
                      "px-2 py-0.5 text-[10px] tracking-widest " +
                      (assetMode === m
                        ? "bg-bb-amber font-semibold text-black"
                        : "text-bb-muted hover:text-bb-amber")
                    }
                  >
                    {m.toUpperCase()}
                  </button>
                ))}
              </div>
              <div className="min-w-0 flex-1">
                <ChartControls />
              </div>
            </div>
            <div className="flex min-h-0 flex-1">
              {/* Permanent HUD sidebar: overlays/sim/theta live OFF the
                  canvas so they never occlude drag handles or contours.
                  Options math only — hidden for the share ticket. */}
              {optionsMode && (
                <aside className="shrink-0 border-r border-bb-border">
                  <ChartHud designer={designer} barsRef={sharedBars} variant="sidebar" />
                </aside>
              )}
              <div className="relative min-h-0 min-w-0 flex-1">
                <CandlePane designer={designer} hudVariant="none" />
                {optionsMode && <LegRail designer={designer} />}
              </div>
              {optionsMode && chainOpen && (
                <aside className="w-72 shrink-0 border-l border-bb-border">
                  <ChainPanel />
                </aside>
              )}
            </div>
          </main>

          <PositionsDrawer />

          {optionsMode ? (
            <section className="grid max-h-[40vh] shrink-0 auto-rows-[16rem] grid-cols-2 gap-px overflow-y-auto xl:grid-cols-3">
              <StrategyPanel designer={designer} />
              <SizingPanel designer={designer} />
              <OrderPanel designer={designer} />
            </section>
          ) : (
            <section className="grid max-h-[44vh] shrink-0 auto-rows-[19rem] grid-cols-1 gap-px overflow-y-auto md:grid-cols-2">
              <EquityTicket />
            </section>
          )}
        </>
      )}
    </div>
  );
}
