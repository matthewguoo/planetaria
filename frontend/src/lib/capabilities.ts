/**
 * ACCOUNT CAPABILITIES — one derivation, honoured by every pane.
 *
 * What an account may place is declared once in the risk settings
 * (`options_level`, `equity_long_only`; edited on the ACCOUNT page) and
 * enforced by the server. This module turns those settings into the flags
 * the UI uses to HIDE the unsupported shapes — an IRA at options level 2
 * never sees spread presets, sell buttons on the chain, theta templates or
 * a SHORT side, instead of seeing them and being refused. The live server
 * floors itself at level 2 regardless of the stored value, exactly as the
 * server's own gate does.
 */

import type { RiskSettings } from "./api";
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
};

export function capabilitiesFrom(risk: RiskSettings | undefined, live: boolean): Capabilities {
  const rawLevel = risk?.options_level ?? 3;
  const optionsLevel = live ? Math.min(rawLevel, 2) : rawLevel;
  return {
    loaded: !!risk,
    live,
    optionsLevel,
    optionsAllowed: optionsLevel >= 2,
    spreadsAllowed: optionsLevel >= 3,
    shortsAllowed: risk ? !risk.equity_long_only : false,
  };
}

export function useCapabilities(): Capabilities {
  const risk = useAccountStore((s) => s.account?.risk);
  const { live } = useTradingMode();
  return capabilitiesFrom(risk, live);
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
