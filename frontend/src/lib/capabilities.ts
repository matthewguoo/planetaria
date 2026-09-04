/**
 * ACCOUNT CAPABILITIES — one derivation, honoured by every pane.
 *
 * The server reports the EFFECTIVE risk settings: the stored preference
 * capped by what the capabilities probe verified at the broker (or the
 * broker's own flags when unprobed). This module turns that into the flags
 * the UI uses to HIDE unsupported shapes — an IRA at options level 2 with
 * no shorting never sees spread presets, sell buttons on the chain, theta
 * templates or a SHORT side, instead of seeing them and being refused.
 * The live server floors itself at level 2 until a probe has run.
 */

import type { CapabilitiesSummary, RiskSettings } from "./api";
import { useAccountStore, useTradingMode } from "../store/accountStore";
import { STRATEGIES, type StrategyKind } from "../store/strategyStore";

export type Capabilities = {
  /** False until the first account fetch — treat as "assume nothing". */
  loaded: boolean;
  live: boolean;
  /** 0/1 none, 2 long single-leg, 3 spreads and defined-risk structures. */
  optionsLevel: number;
  optionsAllowed: boolean;
  spreadsAllowed: boolean;
  shortsAllowed: boolean;
  /** A probe has run for this account. */
  verified: boolean;
  probedAt: string | null;
  /** Where the effective level comes from: probe / broker / default. */
  levelSource: string;
  /** Set when the probe left a position it could not flatten. */
  manualAction: string | null;
};

export function capabilitiesFrom(
  risk: RiskSettings | undefined,
  live: boolean,
  caps?: CapabilitiesSummary | null,
): Capabilities {
  const rawLevel = risk?.options_level ?? 3;
  const verified = !!caps?.probed_at;
  // The server already applies the verified ceiling; the client floor is
  // only for a live server nothing has probed yet.
  const optionsLevel = live && !verified ? Math.min(rawLevel, 2) : rawLevel;
  const shortsVerifiedOff = caps?.derived?.equity_shorts === false;
  return {
    loaded: !!risk,
    live,
    optionsLevel,
    optionsAllowed: optionsLevel >= 2,
    spreadsAllowed: optionsLevel >= 3,
    shortsAllowed: risk ? !risk.equity_long_only && !shortsVerifiedOff : false,
    verified,
    probedAt: caps?.probed_at ?? null,
    levelSource: caps?.sources?.options_level ?? "default",
    manualAction: caps?.manual_action ?? null,
  };
}

export function useCapabilities(): Capabilities {
  const risk = useAccountStore((s) => s.account?.risk);
  const caps = useAccountStore((s) => s.account?.capabilities);
  const { live } = useTradingMode();
  return capabilitiesFrom(risk, live, caps);
}

/** Is this leg shape placeable at the level? Level 2 = exactly one long
 * leg (the server's rule, verbatim); level 3 = anything the broker takes. */
export function legsAllowed(legs: readonly { side: number }[], level: number): boolean {
  if (level >= 3) return true;
  if (level === 2) return legs.length === 1 && legs[0].side > 0;
  return false;
}

export function presetAllowed(kind: StrategyKind, level: number): boolean {
  return legsAllowed(STRATEGIES[kind].legs, level);
}

export const OPTIONS_LEVEL_LABEL: Record<number, string> = {
  0: "NONE",
  1: "L1 · covered only (no manual shapes)",
  2: "L2 · long calls / puts",
  3: "L3 · spreads + defined risk",
};

/** One-line summary for fold titles: "L2 · long-only · cash · fractional". */
export function capabilitiesSummaryLine(caps: CapabilitiesSummary | null | undefined): string {
  if (!caps) return "";
  const d = caps.derived ?? {};
  const parts: string[] = [];
  if (d.options_level != null) parts.push(`L${d.options_level}`);
  if (d.equity_shorts === false) parts.push("long-only");
  if (d.equity_shorts === true) parts.push("shorts");
  if (d.cash_account === true) parts.push("cash");
  if (d.cash_account === false) parts.push("margin");
  if (d.fractional === true) parts.push("fractional");
  if (d.extended_hours === true) parts.push("ext-hours");
  return parts.join(" · ");
}
