/**
 * The ops console — the default app. A portal for running a book of
 * strategies, not a trading terminal.
 *
 * Four destinations, and they are peers:
 *   ACCOUNT     the money — balances, exposure, open and closed P&L
 *   STRATEGIES  the book — instances, params, journals, performance
 *   MARKET      what the engine can see — clock, majors, calendar, tape
 *   SYSTEM      whether the machine works — health, tasks, feeds, call flow
 *
 * SYSTEM earns equal billing because an engine that trades unattended fails
 * silently by default: a dead feed and a quiet market produce the same empty
 * screen everywhere else in the app.
 *
 * Nothing here knows what a particular strategy does. A console that knows
 * what `earnings_reaction` is has to be edited every time a strategy is
 * added, and the previous version had exactly that problem — a whole second
 * entry point hardcoded to one kind. A new strategy appears here by being
 * registered in app/strategies/__init__.py and needs no frontend change.
 *
 * The discretionary options cockpit is filed away at /terminal.html, linked
 * from SYSTEM.
 */

import { useEffect, useState } from "react";
import { AccountPage } from "./components/Account/AccountPage";
import { EnforcementBanner } from "./components/EnforcementBanner";
import FundPage from "./components/Fund/FundPage";
import { Header } from "./components/Header";
import MarketPage from "./components/Market/MarketPage";
import StrategiesPage from "./components/Strategies/StrategiesPage";
import SystemPage from "./components/System/SystemPage";
import { useAccountStore } from "./store/accountStore";
import { useUiStore } from "./store/uiStore";

/** Account and positions only. The terminal additionally pumps the options
 * chain; nothing here renders one, so nothing here fetches one. */
function useDataPumps() {
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const [cadence, setCadence] = useState({ account: 30_000, positions: 5_000 });

  useEffect(() => {
    import("./lib/api").then(({ getFeedSettings }) =>
      getFeedSettings()
        .then((cfg) =>
          setCadence({
            account: cfg.account_poll_s * 1000,
            positions: cfg.positions_poll_s * 1000,
          }),
        )
        .catch(() => {}),
    );
  }, []);

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

export default function OpsApp() {
  useDataPumps();
  const view = useUiStore((s) => s.view);

  return (
    <div className="flex h-full flex-col gap-px bg-bb-black p-px">
      <Header />
      <EnforcementBanner />
      {view === "account" ? (
        <AccountPage />
      ) : view === "strategies" ? (
        <StrategiesPage />
      ) : view === "market" ? (
        <MarketPage />
      ) : view === "system" ? (
        <SystemPage />
      ) : (
        <FundPage />
      )}
    </div>
  );
}
