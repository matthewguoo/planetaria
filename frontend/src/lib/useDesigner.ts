/**
 * Single derivation point for the designer panels: current legs, exits,
 * sizing, probabilities. Everything client-computed (mirror); server numbers
 * are re-validated at order time.
 */

import { useMemo } from "react";
import { computeProbabilitiesClient, computeSizingClient } from "./analytics";
import { positionEntryCost } from "./optionsMath";
import { useAccountStore } from "../store/accountStore";
import {
  buildLegs,
  hoursToExpiry as calcHte,
  useStrategyStore,
  type StrategyLeg,
} from "../store/strategyStore";
import { useTradingStore } from "../store/tradingStore";

export type Designer = {
  ready: boolean;
  demo: boolean;
  legs: StrategyLeg[] | null;
  spot: number;
  entry: number;
  hoursToExpiry: number;
  tpPremium: number | null;
  slPremium: number | null;
  qty: number;
  autoQty: number;
  sizing: ReturnType<typeof computeSizingClient> | null;
  probabilities: ReturnType<typeof computeProbabilitiesClient> | null;
  equity: number;
};

export function useDesigner(): Designer {
  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const kind = useStrategyStore((s) => s.kind);
  const strikes = useStrategyStore((s) => s.strikes);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const qtyOverride = useStrategyStore((s) => s.qty);
  const quote = useTradingStore((s) => s.quote);
  const account = useAccountStore((s) => s.account);

  return useMemo(() => {
    const legs = buildLegs({ chain, expiry, kind, strikes });
    const spot = quote?.mid || chain?.spot || 0;
    const equity = account?.equity ?? 0;
    const risk = account?.risk;
    const empty: Designer = {
      ready: false,
      demo: chain?.demo ?? false,
      legs: null,
      spot,
      entry: 0,
      hoursToExpiry: 0,
      tpPremium: null,
      slPremium: null,
      qty: 0,
      autoQty: 0,
      sizing: null,
      probabilities: null,
      equity,
    };
    if (!legs || !expiry || spot <= 0) return empty;

    const entry = positionEntryCost(legs);
    if (entry <= 0) return { ...empty, legs };
    const hte = calcHte(expiry);
    const tpPremium = entry * (1 + tpPct);
    const slPremium = entry * (1 - slPct);

    const sizing = computeSizingClient(
      legs,
      equity,
      risk?.max_loss_pct ?? 0.02,
      slPremium,
      risk?.bp_cap_pct ?? 0.25,
    );
    const probabilities = computeProbabilitiesClient(legs, spot, hte, tpPremium, slPremium);
    const autoQty = sizing.contracts;
    const qty = qtyOverride > 0 ? Math.min(qtyOverride, Math.max(autoQty, 0)) : autoQty;

    return {
      ready: true,
      demo: chain?.demo ?? false,
      legs,
      spot,
      entry,
      hoursToExpiry: hte,
      tpPremium,
      slPremium,
      qty,
      autoQty,
      sizing,
      probabilities,
      equity,
    };
  }, [chain, expiry, kind, strikes, tpPct, slPct, qtyOverride, quote, account]);
}
