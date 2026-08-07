/**
 * The ops console — the default app.
 *
 * Two jobs and nothing else: manage the live broker connection, and run
 * strategies. There is no chart, no options designer and no per-strategy
 * surface here, deliberately. A console that knows what `earnings_reaction`
 * is has to be edited every time a strategy is added, and the previous
 * version of this app had exactly that problem: a whole second entry point
 * hardcoded to one strategy kind.
 *
 * What replaced it is the STRATEGIES page, which is kind-agnostic all the
 * way down — params are JSON, the decision journal renders whatever the
 * strategy journaled, and SOURCE reads the module the engine is actually
 * running. A new strategy shows up here by being registered in
 * app/strategies/__init__.py and needs no frontend change at all.
 *
 * The options terminal still exists, at /terminal.html.
 */

import { useEffect, useState } from "react";
import { AccountPage } from "./components/Account/AccountPage";
import { EnforcementBanner } from "./components/EnforcementBanner";
import { Header } from "./components/Header";
import MonitorPage from "./components/Monitor/MonitorPage";
import StrategiesPage from "./components/Strategies/StrategiesPage";
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
  const setView = useUiStore((s) => s.setView);

  // The store's default view is the terminal's chart, which this shell does
  // not render. Land on STRATEGIES instead — it is what the console is for.
  useEffect(() => {
    if (view === "terminal") setView("strategies");
  }, [view, setView]);

  return (
    <div className="flex h-full flex-col gap-px bg-bb-black p-px">
      <Header variant="ops" />
      <EnforcementBanner />
      {view === "account" ? (
        <AccountPage />
      ) : view === "monitor" ? (
        <MonitorPage />
      ) : (
        <StrategiesPage />
      )}
    </div>
  );
}
