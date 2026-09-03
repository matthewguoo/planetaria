/**
 * ONE derivation of the equity swing plan from the ticket store + market
 * state. The ticket panel (desktop and phone) and the chart overlay both
 * read this, so the stop the chart draws IS the stop the ticket submits,
 * and a drag on the chart is just another setter on the same store.
 */

import type { Account } from "./api";
import {
  autoHoldDays,
  capitalUsd,
  dailySigmaPct,
  equityExits,
  holdDaysUntil,
  holdSigmaPct,
  pTargetFirst,
  rr,
  sharesForRisk,
  suggestedStopPct,
  ticketPrice,
  tradingDateAhead,
  type EquityExits,
} from "./equityMath";
import { realizedVolAnnualized } from "./indicators";
import type { Bars } from "../components/Chart/scales";
import type { EquityTicketState } from "../store/equityTicketStore";
import { freshSpot, quoteIsStale, TF_MS, type Quote, type Timeframe } from "../store/tradingStore";

export type EquityPlan = {
  price: number;
  quoteFresh: boolean;
  halfSpread: number | null;
  exits: EquityExits;
  shares: number;
  autoShares: number;
  notional: number;
  maxLoss: number;
  rrMult: number | null;
  /** Driftless P(target touched before stop); null without a target. */
  pTarget: number | null;
  riskBudget: number;
  acctEquity: number;
  maxLossCapUsd: number | null;
  dailyCapUsd: number | null;
  /** Symbol vol context from the chart's own bars. */
  dSigma: number;
  holdDays: number;
  /** Where the automatic horizon put the exit, or the explicit date. */
  timeStopDate: string;
  timeStopAuto: boolean;
  stopSuggestion: number | null;
  noiseSigma: number;
  stopInsideNoise: boolean;
  /** 1σ / 2σ hold-horizon moves as target % (for the σ chips). */
  sigmaTargets: [number, number] | null;
};

const DEFAULT_HOLD_DAYS = 5;

export function deriveEquityPlan(args: {
  ticket: Pick<
    EquityTicketState,
    "side" | "riskPct" | "slPct" | "tpOn" | "tpPct" | "sharesOverride" | "timeStopDate" | "autoTimeStop"
  >;
  quote: Quote | null;
  bars: Bars;
  tf: Timeframe;
  account: Account | null;
  now?: Date;
}): EquityPlan {
  const { ticket, quote, bars, tf, account } = args;
  const now = args.now ?? new Date();
  const quoteFresh = !!quote && quote.mid > 0 && !quoteIsStale(quote, now.getTime());
  const price = ticketPrice(quote, quoteFresh, ticket.side, freshSpot(quote, 0, now.getTime()));
  const halfSpread =
    quote && quote.ask > 0 && quote.bid > 0 && quote.ask >= quote.bid ? (quote.ask - quote.bid) / 2 : null;

  const exits = equityExits(price, ticket.side, ticket.slPct / 100, ticket.tpOn ? ticket.tpPct / 100 : null);
  const acctEquity = account?.equity ?? 0;
  const maxLossCapUsd = account?.risk ? account.equity * account.risk.max_loss_pct : null;
  const dailyCapUsd = account?.risk ? account.equity * account.risk.daily_loss_pct : null;
  const riskBudget = (acctEquity * ticket.riskPct) / 100;

  const rv = bars.n > 30 ? realizedVolAnnualized(bars, 30, TF_MS[tf] / 60_000) : 0;
  const dSigma = dailySigmaPct(rv);

  // Horizon: explicit date wins; else the automatic one the stop implies;
  // else the default swing horizon while the tape is too short to know.
  let holdDays: number;
  let timeStopDate = ticket.timeStopDate;
  let timeStopAuto = false;
  if (ticket.timeStopDate) {
    holdDays = holdDaysUntil(ticket.timeStopDate, now);
  } else if (ticket.autoTimeStop) {
    holdDays = autoHoldDays(ticket.slPct, dSigma) ?? DEFAULT_HOLD_DAYS;
    timeStopDate = tradingDateAhead(holdDays, now);
    timeStopAuto = true;
  } else {
    holdDays = DEFAULT_HOLD_DAYS;
  }

  const stopSuggestion = suggestedStopPct(dSigma, holdDays);
  const noiseSigma = dSigma > 0 ? holdSigmaPct(dSigma, holdDays) : 0;
  const stopInsideNoise = noiseSigma > 0 && ticket.slPct < noiseSigma;
  const sigmaTargets: [number, number] | null =
    noiseSigma > 0
      ? [Math.round(noiseSigma * 2) / 2, Math.round(noiseSigma * 4) / 2]
      : null;

  const autoShares = price > 0 ? sharesForRisk(riskBudget, exits.entry, exits.sl) : 0;
  const shares =
    ticket.sharesOverride > 0 ? Math.min(ticket.sharesOverride, Math.max(autoShares, 1)) : autoShares;
  const notional = capitalUsd(price, shares, ticket.side);
  const maxLoss = (exits.entry - exits.sl) * shares;

  return {
    price,
    quoteFresh,
    halfSpread,
    exits,
    shares,
    autoShares,
    notional,
    maxLoss,
    rrMult: rr(exits.entry, exits.tp, exits.sl),
    pTarget: price > 0 ? pTargetFirst(exits) : null,
    riskBudget,
    acctEquity,
    maxLossCapUsd,
    dailyCapUsd,
    dSigma,
    holdDays,
    timeStopDate,
    timeStopAuto,
    stopSuggestion,
    noiseSigma,
    stopInsideNoise,
    sigmaTargets,
  };
}
