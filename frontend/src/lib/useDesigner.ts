/**
 * Single derivation point for the designer panels: current legs, exits,
 * sizing, probabilities. Everything client-computed (mirror); server numbers
 * are re-validated at order time.
 */

import { useMemo } from "react";
import { computeProbabilitiesClient, computeSizingClient } from "./analytics";
import {
  positionEntryCost,
  positionValue,
  TRADING_HOURS_PER_YEAR,
  type Smiles,
} from "./optionsMath";
import { useAccountStore } from "../store/accountStore";
import {
  buildLegs,
  hoursToExpiry as calcHte,
  smileFromChain,
  timeStopHoursFromEt,
  useStrategyStore,
  type StrategyLeg,
} from "../store/strategyStore";
import { freshSpot, useTradingStore } from "../store/tradingStore";

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
  /** Live chain smile for the active expiry (scenario model input). */
  smiles: Smiles | null;
  /** Trading hours from now until the configured time stop. */
  timeStopHours: number;
  /** Estimated round-trip bid/ask cost, dollars per contract-set. */
  frictionPerSet: number;
  /** Discrete risks a P/L surface can't show (assignment, pin). */
  warnings: string[];
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
  const rights = useStrategyStore((s) => s.rights);
  const sides = useStrategyStore((s) => s.sides);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const qtyOverride = useStrategyStore((s) => s.qty);
  const timeStopEt = useStrategyStore((s) => s.timeStopEt);
  const quote = useTradingStore((s) => s.quote);
  const account = useAccountStore((s) => s.account);

  return useMemo(() => {
    const legs = buildLegs({ chain, expiry, kind, strikes, ratios, rights, sides });
    const spot = freshSpot(quote, chain?.spot ?? 0);
    // Manual options trades size against the OPTIONS book envelope when
    // books are enabled — the $100k paper account is not the discretionary
    // bankroll, and the share book is a separate bankroll again.
    const book = account?.manual_book?.enabled ? account.manual_book.option : null;
    const equity = book ? book.equity_usd : account?.equity ?? 0;
    const risk = account?.risk;
    const smiles = smileFromChain(chain, expiry);
    const timeStopHours = timeStopHoursFromEt(timeStopEt);
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
      smiles,
      timeStopHours,
      frictionPerSet: 0,
      warnings: [],
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
      (book ? book.max_loss_pct : risk?.max_loss_pct) ?? 0.02,
      slPremium,
      risk?.bp_cap_pct ?? 0.25,
      spot,
    );
    const probabilities = computeProbabilitiesClient(legs, spot, hte, tpPremium, slPremium);
    const autoQty = sizing.contracts;
    const qty = qtyOverride > 0 ? Math.min(qtyOverride, Math.max(autoQty, 0)) : autoQty;

    // Round-trip bid/ask friction per contract-set: half-spread paid on the
    // way in AND the way out, per leg, weighted by ratio.
    const frictionPerSet =
      legs.reduce((acc, leg) => acc + leg.qty * leg.halfSpread * 2, 0) * 100;

    // Discrete risks no surface can show — flag them instead.
    const warnings: string[] = [];
    const maxSpreadPct = risk?.max_spread_pct ?? 0.15;
    for (const leg of legs) {
      if (leg.entry > 0 && leg.halfSpread / leg.entry > maxSpreadPct) {
        warnings.push(
          `illiquid leg ${leg.strike}${leg.right}: half-spread ${Math.round((leg.halfSpread / leg.entry) * 100)}% of mid (server will reject > ${Math.round(maxSpreadPct * 100)}%)`,
        );
      }
    }
    for (const leg of legs) {
      if (leg.side >= 0) continue;
      const intrinsic = Math.max(leg.right === "C" ? spot - leg.strike : leg.strike - spot, 0);
      if (intrinsic > 0 && leg.entry - intrinsic < 0.05) {
        warnings.push(
          `early assignment risk: short ${leg.strike}${leg.right} is ITM with ≈no extrinsic value`,
        );
      }
      if (hte <= 6.5 && Math.abs(spot - leg.strike) / spot < 0.003) {
        warnings.push(`pin risk: short ${leg.strike}${leg.right} sits at spot into expiry`);
      }
    }
    if (hte > 6.5) {
      warnings.push("overnight gap risk: stops cannot protect across sessions");
    }

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
      smiles,
      timeStopHours,
      frictionPerSet,
      warnings,
      qty,
      autoQty,
      sizing,
      probabilities,
      equity,
    };
  }, [chain, expiry, kind, strikes, ratios, rights, sides, tpPct, slPct, qtyOverride, timeStopEt, quote, account]);
}
