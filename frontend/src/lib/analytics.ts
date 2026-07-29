/**
 * Client-side analytics assembled from the math mirror — instant feedback for
 * panels while dragging. Server (/api/analytics/*, order validation) stays
 * authoritative at order time. Mirrors backend/app/services/analytics.py.
 */

import {
  bpPerSetEstimate,
  nakedShortUnits,
  breakevens,
  payoffAtExpiry,
  positionEntryCost,
  positionIv,
  premiumBarrierUnderlying,
  probAboveAtExpiry,
  probTouch,
  structuralMaxLoss,
  terminalEv,
  TRADING_HOURS_PER_YEAR,
  type Leg,
} from "./optionsMath";

export type ClientProbabilities = {
  pProfitExpiry: number;
  breakevens: number[];
  pTouchTp: number | null;
  pTouchSl: number | null;
  tpBarrier: number | null;
  slBarrier: number | null;
  evPerContract: number;
  rewardPerContract: number | null;
  riskPerContract: number | null;
  rr: number | null;
  sigmaUsed: number;
};

export function computeProbabilitiesClient(
  legs: Leg[],
  spot: number,
  hoursToExpiry: number,
  tpPremium: number | null,
  slPremium: number | null,
): ClientProbabilities {
  const tau = Math.max(hoursToExpiry, 0) / TRADING_HOURS_PER_YEAR;
  const sigma = positionIv(legs);
  const entry = positionEntryCost(legs);
  const lo = spot * 0.75;
  const hi = spot * 1.25;
  const bes = breakevens(legs, lo, hi);

  const edges = [lo, ...bes, hi];
  let pProfit = 0;
  for (let i = 0; i < edges.length - 1; i++) {
    const mid = 0.5 * (edges[i] + edges[i + 1]);
    if (payoffAtExpiry(legs, mid) > 0) {
      pProfit +=
        probAboveAtExpiry(spot, edges[i], tau, sigma) -
        probAboveAtExpiry(spot, edges[i + 1], tau, sigma);
    }
  }

  const tauEval = tau / 2;
  let pTp: number | null = null;
  let pSl: number | null = null;
  let tpBarrier: number | null = null;
  let slBarrier: number | null = null;
  if (tpPremium !== null) {
    tpBarrier = premiumBarrierUnderlying(legs, tpPremium, tauEval, lo, hi);
    if (tpBarrier !== null) pTp = probTouch(spot, tpBarrier, tau, sigma);
  }
  if (slPremium !== null) {
    slBarrier = premiumBarrierUnderlying(legs, slPremium, tauEval, lo, hi);
    if (slBarrier !== null) pSl = probTouch(spot, slBarrier, tau, sigma);
  }

  const ev = terminalEv(legs, spot, tau, sigma, tpPremium, slPremium) * 100;
  const reward = tpPremium !== null ? (tpPremium - entry) * 100 : null;
  const risk = slPremium !== null ? (entry - slPremium) * 100 : null;

  return {
    pProfitExpiry: pProfit,
    breakevens: bes.map((b) => Math.round(b * 100) / 100),
    pTouchTp: pTp,
    pTouchSl: pSl,
    tpBarrier,
    slBarrier,
    evPerContract: ev,
    rewardPerContract: reward,
    riskPerContract: risk,
    rr: reward !== null && risk !== null && risk > 0 ? reward / risk : null,
    sigmaUsed: sigma,
  };
}

export type ClientSizing = {
  contracts: number;
  entryCost: number;
  maxLossAtStop: number;
  maxLossStructural: number;
  buyingPowerPct: number;
  perContractRisk: number;
  /** What the stop-risk budget ALONE would allow — when BP binds below this,
   * the gap is the price of undefined risk (naked short margin). */
  riskBudgetContracts: number;
  reasons: string[];
};

export function computeSizingClient(
  legs: Leg[],
  accountEquity: number,
  maxLossPct: number,
  slPremium: number,
  bpCapPct: number,
  spot = 0,
): ClientSizing {
  const reasons: string[] = [];
  const entry = positionEntryCost(legs); // +debit / -credit, per share
  const empty = { contracts: 0, entryCost: 0, maxLossAtStop: 0, maxLossStructural: 0,
                  buyingPowerPct: 0, perContractRisk: 0, riskBudgetContracts: 0 };
  if (Math.abs(entry) < 0.01) {
    return { ...empty, reasons: ["net premium is zero - nothing to size"] };
  }
  // Stop-based risk is sign-agnostic: SL sits below entry on the
  // position-value axis for debit AND credit structures.
  const perSetRisk = Math.max((entry - slPremium) * 100, 0);
  if (perSetRisk <= 0) {
    return { ...empty, reasons: ["stop-loss premium must be below entry"] };
  }
  // Capital per set is the BROKER's number, estimated: debit for longs,
  // structural width for defined risk, cash-secured strike value for
  // uncovered short puts (verified against Alpaca's own cost basis).
  const structural = structuralMaxLoss(legs);
  const perSetCost = spot > 0 ? bpPerSetEstimate(legs, spot)
    : entry > 0 ? entry * 100
    : structural !== null ? structural * 100
    : perSetRisk * 3;
  const perSetStructural =
    structural !== null ? structural * 100 : Math.max(perSetCost, perSetRisk * 3);
  if (structural === null) {
    reasons.push("undefined risk (net short calls) - stop + margin sizing only");
  }
  const budget = accountEquity * maxLossPct;
  const riskBudgetContracts = Math.max(Math.floor(budget / perSetRisk), 0);
  let contracts = riskBudgetContracts;
  if (contracts < 1) {
    reasons.push(`risk/contract $${perSetRisk.toFixed(0)} exceeds budget $${budget.toFixed(0)}`);
  }
  const bpBudget = accountEquity * bpCapPct;
  if (contracts * perSetCost > bpBudget && perSetCost > 0) {
    contracts = Math.min(contracts, Math.floor(bpBudget / perSetCost));
    const nakedPuts = nakedShortUnits(legs, "P");
    if (nakedPuts > 0) {
      reasons.push(
        `BP-bound: uncovered short put is cash-secured (~$${(perSetCost / 1000).toFixed(1)}k/set at the broker). ` +
        `Stop-risk budget alone allows ${riskBudgetContracts} — add a protective put wing to size by stop risk, ` +
        `or raise BP CAP (ACCOUNT tab)`,
      );
    } else {
      reasons.push(`capped by buying-power limit ${(bpCapPct * 100).toFixed(0)}%`);
    }
  }
  return {
    contracts,
    entryCost: contracts * perSetCost,
    maxLossAtStop: contracts * perSetRisk,
    maxLossStructural: contracts * perSetStructural,
    buyingPowerPct: accountEquity ? (contracts * perSetCost) / accountEquity : 0,
    perContractRisk: perSetRisk,
    riskBudgetContracts,
    reasons,
  };
}
