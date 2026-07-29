import { useEffect, useState } from "react";
import { AccountPage } from "./components/Account/AccountPage";
import { Header } from "./components/Header";
import { CandlePane } from "./components/Chart/CandlePane";
import { ChainPanel } from "./components/Chart/ChainPanel";
import { ChartControls } from "./components/Chart/ChartControls";
import { MobileApp } from "./components/Mobile/MobileApp";
import { OrderPanel } from "./components/Panels/OrderPanel";
import { SizingPanel } from "./components/Panels/SizingPanel";
import { StrategyPanel } from "./components/Panels/StrategyPanel";
import { PositionsDrawer } from "./components/Positions/PositionsDrawer";
import { useDesigner } from "./lib/useDesigner";
import { useAccountStore } from "./store/accountStore";
import { useStrategyStore } from "./store/strategyStore";
import { useTradingStore } from "./store/tradingStore";
import { useUiStore } from "./store/uiStore";

// Below this width the dedicated phone layout takes over (chart-first,
// bottom-sheet panels — see components/Mobile/).
const MIN_WIDTH = 640;

function usePhoneViewport(): boolean {
  const [phone, setPhone] = useState(window.innerWidth < MIN_WIDTH);
  useEffect(() => {
    const onResize = () => setPhone(window.innerWidth < MIN_WIDTH);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return phone;
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

// ?unlock forces the desktop layout in small panes (QA/testing).
const UNLOCKED = new URLSearchParams(window.location.search).has("unlock");

export default function App() {
  const phone = usePhoneViewport();
  useDataPumps();
  const designer = useDesigner();
  const view = useUiStore((s) => s.view);
  const chainOpen = useUiStore((s) => s.chainOpen);

  if (phone && !UNLOCKED) return <MobileApp />;

  return (
    <div className="flex h-full flex-col gap-px bg-bb-black p-px">
      <Header />

      {view === "account" ? (
        <AccountPage />
      ) : (
        <>
          <main className="panel flex min-h-0 flex-1 flex-col">
            <ChartControls />
            <div className="flex min-h-0 flex-1">
              <div className="relative min-h-0 min-w-0 flex-1">
                <CandlePane designer={designer} />
              </div>
              {chainOpen && (
                <aside className="w-72 shrink-0 border-l border-bb-border">
                  <ChainPanel />
                </aside>
              )}
            </div>
          </main>

          <PositionsDrawer />

          <section className="grid max-h-[40vh] shrink-0 auto-rows-[16rem] grid-cols-2 gap-px overflow-y-auto xl:grid-cols-3">
            <StrategyPanel designer={designer} />
            <SizingPanel designer={designer} />
            <OrderPanel designer={designer} />
          </section>
        </>
      )}
    </div>
  );
}
