/**
 * One poll per held symbol for its holding detail (contract facts, live
 * quote, entry time), shared by every component that wants it — the chart
 * overlay, the position panel and the phone sheets read the same row
 * instead of each polling the server.
 */

import { useEffect } from "react";
import { create } from "zustand";
import { getHoldingDetail, type HoldingDetailWithEntry } from "./api";

type State = {
  rows: Record<string, HoldingDetailWithEntry>;
  /** Subscriber counts per symbol; a symbol polls while it has any. */
  subs: Record<string, number>;
};

export const useHoldingDetailStore = create<State>(() => ({ rows: {}, subs: {} }));

const timers = new Map<string, number>();
const POLL_MS = 5_000;

async function fetchOne(symbol: string) {
  try {
    const row = await getHoldingDetail(symbol);
    if (useHoldingDetailStore.getState().subs[symbol]) {
      useHoldingDetailStore.setState((s) => ({ rows: { ...s.rows, [symbol]: row } }));
    }
  } catch {
    /* the consumers show what the plan or the broker row already knows */
  }
}

function subscribe(symbol: string) {
  const subs = { ...useHoldingDetailStore.getState().subs };
  subs[symbol] = (subs[symbol] ?? 0) + 1;
  useHoldingDetailStore.setState({ subs });
  if (!timers.has(symbol)) {
    void fetchOne(symbol);
    timers.set(symbol, window.setInterval(() => void fetchOne(symbol), POLL_MS));
  }
}

function unsubscribe(symbol: string) {
  const subs = { ...useHoldingDetailStore.getState().subs };
  subs[symbol] = Math.max((subs[symbol] ?? 1) - 1, 0);
  if (!subs[symbol]) {
    delete subs[symbol];
    const id = timers.get(symbol);
    if (id !== undefined) window.clearInterval(id);
    timers.delete(symbol);
  }
  useHoldingDetailStore.setState({ subs });
}

/** The detail row for `symbol` (null until the first poll lands, or when
 * there is no symbol — a multi-leg plan has no single holding). */
export function useHoldingDetail(symbol: string | null): HoldingDetailWithEntry | null {
  useEffect(() => {
    if (!symbol) return;
    subscribe(symbol);
    return () => unsubscribe(symbol);
  }, [symbol]);
  return useHoldingDetailStore((s) => (symbol ? s.rows[symbol] ?? null : null));
}
