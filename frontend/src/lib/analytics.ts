/**
 * Client-side analytics assembled from the math mirror — instant feedback for
 * panels while dragging. Server (/api/analytics/*, order validation) stays
 * authoritative at order time. Mirrors backend/app/services/analytics.py.
 */

import {
  breakevens,
  payoffAtExpiry,
  positionEntryCost,
  positionIv,
  premiumBarrierUnderlying,
  probAboveAtExpiry,
  probTouch,
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
  buyingPowerPct: number;
  perContractRisk: number;
  reasons: string[];
};

export function computeSizingClient(
  legs: Leg[],
  accountEquity: number,
  maxLossPct: number,
  slPremium: number,
  bpCapPct: number,
): ClientSizing {
  const reasons: string[] = [];
  const entry = positionEntryCost(legs);
  if (entry <= 0) {
    return { contracts: 0, entryCost: 0, maxLossAtStop: 0, buyingPowerPct: 0, perContractRisk: 0,
             reasons: ["net credit structures not supported in v1"] };
  }
  const perSetCost = entry * 100;
  const perSetRisk = Math.max((entry - slPremium) * 100, 0);
  if (perSetRisk <= 0) {
    return { contracts: 0, entryCost: 0, maxLossAtStop: 0, buyingPowerPct: 0, perContractRisk: 0,
             reasons: ["stop-loss premium must be below entry cost"] };
  }
  const budget = accountEquity * maxLossPct;
  let contracts = Math.floor(budget / perSetRisk);
  if (contracts < 1) {
    reasons.push(`risk/contract $${perSetRisk.toFixed(0)} exceeds budget $${budget.toFixed(0)}`);
    contracts = 0;
  }
  const bpBudget = accountEquity * bpCapPct;
  if (contracts * perSetCost > bpBudget && perSetCost > 0) {
    const capped = Math.floor(bpBudget / perSetCost);
    if (capped < contracts) {
      contracts = capped;
      reasons.push(`capped by buying-power limit ${(bpCapPct * 100).toFixed(0)}%`);
    }
  }
  return {
    contracts,
    entryCost: contracts * perSetCost,
    maxLossAtStop: contracts * perSetRisk,
    buyingPowerPct: accountEquity ? (contracts * perSetCost) / accountEquity : 0,
    perContractRisk: perSetRisk,
    reasons,
  };
}
