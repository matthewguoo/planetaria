/**
 * Single derivation point for the designer panels: current legs, exits,
 * sizing, probabilities. Everything client-computed (mirror); server numbers
 * are re-validated at order time.
 */

import { useMemo } from "react";
import { computeProbabilitiesClient, computeSizingClient } from "./analytics";
import { positionEntryCost, positionValue, TRADING_HOURS_PER_YEAR } from "./optionsMath";
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
  /** Non-null when the CURRENT model value already sits at/beyond an exit —
   * entering now would be closed by the enforcer immediately. */
  instantExit: "tp" | "sl" | null;
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
  const ratios = useStrategyStore((s) => s.ratios);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const qtyOverride = useStrategyStore((s) => s.qty);
  const quote = useTradingStore((s) => s.quote);
  const account = useAccountStore((s) => s.account);

  return useMemo(() => {
    const legs = buildLegs({ chain, expiry, kind, strikes, ratios });
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
      instantExit: null,
      qty: 0,
      autoQty: 0,
      sizing: null,
      probabilities: null,
      equity,
    };
    if (!legs || !expiry || spot <= 0) return empty;

    // entry is signed: +debit / -credit. TP/SL are premium levels on the same
    // axis — TP above entry, SL below, scaled by |entry| so credit structures
    // (e.g. iron condor) bracket correctly too.
    const entry = positionEntryCost(legs);
    if (Math.abs(entry) < 0.01) return { ...empty, legs };
    const hte = calcHte(expiry);
    const tpPremium = entry + Math.abs(entry) * tpPct;
    const slPremium = entry - Math.abs(entry) * slPct;

    // Sanity guard: with fresh quotes, model value at spot equals entry, so
    // spot always starts inside (SL, TP). If it doesn't (stale quotes, or
    // an inconsistent feed), the enforcer would exit the moment it fills —
    // surface that instead of letting the order through.
    const nowValue = positionValue(legs, spot, Math.max(hte, 0) / TRADING_HOURS_PER_YEAR);
    const instantExit: "tp" | "sl" | null =
      nowValue >= tpPremium ? "tp" : nowValue <= slPremium ? "sl" : null;

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
      instantExit,
      qty,
      autoQty,
      sizing,
      probabilities,
      equity,
    };
  }, [chain, expiry, kind, strikes, ratios, tpPct, slPct, qtyOverride, quote, account]);
}
