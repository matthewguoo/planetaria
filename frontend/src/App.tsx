import { useEffect, useState } from "react";
import { AccountPage } from "./components/Account/AccountPage";
import { Header } from "./components/Header";
import { CandlePane } from "./components/Chart/CandlePane";
import { ChartControls } from "./components/Chart/ChartControls";
import { OrderPanel } from "./components/Panels/OrderPanel";
import { ProbabilityPanel } from "./components/Panels/ProbabilityPanel";
import { SizingPanel } from "./components/Panels/SizingPanel";
import { StrategyPanel } from "./components/Panels/StrategyPanel";
import { PositionsDrawer } from "./components/Positions/PositionsDrawer";
import { useDesigner } from "./lib/useDesigner";
import { useAccountStore } from "./store/accountStore";
import { useStrategyStore } from "./store/strategyStore";
import { useTradingStore } from "./store/tradingStore";
import { useUiStore } from "./store/uiStore";

// The layout auto-sizes fluidly; only genuinely unusable widths (phones in
// portrait) get the lockout.
const MIN_WIDTH = 640;

function useViewportLock(): boolean {
  const [locked, setLocked] = useState(window.innerWidth < MIN_WIDTH);
  useEffect(() => {
    const onResize = () => setLocked(window.innerWidth < MIN_WIDTH);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return locked;
}

function ViewportLockout() {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black">
      <div className="panel max-w-md p-8 text-center">
        <div className="text-lg tracking-widest text-bb-amber">PLANETARIA</div>
        <div className="mt-4 text-bb-muted">
          This terminal needs at least {MIN_WIDTH}px of width.
        </div>
        <div className="mt-2 text-bb-muted">
          Current width: <span className="text-bb-amber">{window.innerWidth}px</span>
        </div>
      </div>
    </div>
  );
}

/** Background data pumps: chain (10s), account (30s), positions (5s). */
function useDataPumps() {
  const symbol = useTradingStore((s) => s.symbol);
  const loadChain = useStrategyStore((s) => s.loadChain);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);

  useEffect(() => {
    void loadChain(symbol);
    const id = window.setInterval(() => void loadChain(symbol), 10_000);
    return () => window.clearInterval(id);
  }, [symbol, loadChain]);

  useEffect(() => {
    void refreshAccount();
    void refreshPositions();
    const acctId = window.setInterval(() => void refreshAccount(), 30_000);
    const posId = window.setInterval(() => void refreshPositions(), 5_000);
    return () => {
      window.clearInterval(acctId);
      window.clearInterval(posId);
    };
  }, [refreshAccount, refreshPositions]);
}

// ?unlock bypasses the viewport lock (QA/testing in small panes).
const UNLOCKED = new URLSearchParams(window.location.search).has("unlock");

export default function App() {
  const locked = useViewportLock();
  useDataPumps();
  const designer = useDesigner();
  const view = useUiStore((s) => s.view);

  if (locked && !UNLOCKED) return <ViewportLockout />;

  return (
    <div className="flex h-full flex-col gap-px bg-bb-black p-px">
      <Header />

      {view === "account" ? (
        <AccountPage />
      ) : (
        <>
          <main className="panel flex min-h-0 flex-1 flex-col">
            <ChartControls />
            <div className="min-h-0 flex-1">
              <CandlePane />
            </div>
          </main>

          <PositionsDrawer />

          <section className="grid max-h-[45vh] shrink-0 auto-rows-[16rem] grid-cols-2 gap-px overflow-y-auto xl:grid-cols-4">
            <StrategyPanel designer={designer} />
            <SizingPanel designer={designer} />
            <ProbabilityPanel designer={designer} />
            <OrderPanel designer={designer} />
          </section>
        </>
      )}
    </div>
  );
}
