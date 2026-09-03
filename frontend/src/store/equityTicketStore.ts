/**
 * The EQUITY swing ticket's inputs, as a store rather than component
 * state: the chart draws the same stop / target / time-stop lines the
 * ticket describes and drags them back into it, and the phone ticket and
 * the desktop panel are two views of one plan. Every setter bumps `rev`;
 * a pending confirm is only valid for the rev it was armed on, so any edit
 * — from either surface — disarms it without an effect.
 */

import { create } from "zustand";
import { exitPositionViewOnAction } from "./uiStore";

export type EquityTicketState = {
  side: 1 | -1;
  /** % of account equity risked between entry and stop. */
  riskPct: number;
  /** Stop distance, % of entry. */
  slPct: number;
  tpOn: boolean;
  /** Target distance, % of entry. */
  tpPct: number;
  /** 0 = auto (risk-sized). */
  sharesOverride: number;
  /** Explicit ET exit date ("" = automatic horizon, see equityMath). */
  timeStopDate: string;
  /** Automatic horizon: hold days derived from the stop vs the symbol's vol. */
  autoTimeStop: boolean;
  extendedHours: boolean;
  rev: number;
  setSide: (v: 1 | -1) => void;
  setRiskPct: (v: number) => void;
  setSlPct: (v: number) => void;
  setTpOn: (v: boolean) => void;
  setTpPct: (v: number) => void;
  setTarget: (on: boolean, pct?: number) => void;
  setSharesOverride: (v: number) => void;
  setTimeStopDate: (v: string) => void;
  setAutoTimeStop: (v: boolean) => void;
  setExtendedHours: (v: boolean) => void;
};

const clamp = (v: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, Math.round(v * 100) / 100));

export const useEquityTicketStore = create<EquityTicketState>((set) => {
  const bump = <K extends keyof EquityTicketState>(key: K, value: EquityTicketState[K]) => {
    exitPositionViewOnAction();
    set((s) => ({ [key]: value, rev: s.rev + 1 }) as Partial<EquityTicketState>);
  };
  return {
    side: 1,
    riskPct: 1.0,
    slPct: 5.0,
    tpOn: false,
    tpPct: 10.0,
    sharesOverride: 0,
    timeStopDate: "",
    autoTimeStop: true,
    extendedHours: false,
    rev: 0,
    setSide: (v) => bump("side", v),
    setRiskPct: (v) => bump("riskPct", clamp(v, 0.1, 10)),
    setSlPct: (v) => bump("slPct", clamp(v, 0.5, 50)),
    setTpOn: (v) => bump("tpOn", v),
    setTpPct: (v) => bump("tpPct", clamp(v, 1, 200)),
    setTarget: (on, pct) => {
      exitPositionViewOnAction();
      set((s) => ({
        tpOn: on,
        tpPct: pct != null ? clamp(pct, 1, 200) : s.tpPct,
        rev: s.rev + 1,
      }));
    },
    setSharesOverride: (v) => bump("sharesOverride", Math.max(0, Math.floor(v))),
    setTimeStopDate: (v) => {
      exitPositionViewOnAction();
      set((s) => ({ timeStopDate: v, autoTimeStop: v === "" ? s.autoTimeStop : false, rev: s.rev + 1 }));
    },
    setAutoTimeStop: (v) => {
      exitPositionViewOnAction();
      set((s) => ({ autoTimeStop: v, timeStopDate: v ? "" : s.timeStopDate, rev: s.rev + 1 }));
    },
    setExtendedHours: (v) => bump("extendedHours", v),
  };
});
