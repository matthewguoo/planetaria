/**
 * Live option NBBO for the contracts on screen (the ticket's legs), fed by
 * the WebSocket `oquote` channel. One quote per OCC symbol, newest wins.
 * Subscriptions are reference-counted here so two panes watching the same
 * leg cost the server one upstream subscription.
 */

import { useEffect } from "react";
import { create } from "zustand";
import { socket } from "../lib/ws";

export type OptionQuote = {
  symbol: string;
  bid: number;
  ask: number;
  mid: number;
  ts: number;
  bid_size?: number | null;
  ask_size?: number | null;
};

type OptionQuoteState = {
  quotes: Record<string, OptionQuote>;
  setQuote: (q: OptionQuote) => void;
};

export const useOptionQuoteStore = create<OptionQuoteState>((set) => ({
  quotes: {},
  setQuote: (q) =>
    set((s) => {
      const prev = s.quotes[q.symbol];
      if (prev && prev.ts > q.ts) return s; // newest wins
      return { quotes: { ...s.quotes, [q.symbol]: q } };
    }),
}));

const refs = new Map<string, { count: number; unsub: () => void }>();

function acquire(symbol: string): void {
  const held = refs.get(symbol);
  if (held) {
    held.count += 1;
    return;
  }
  refs.set(symbol, { count: 1, unsub: socket.subscribe({ channel: "oquote", symbol }) });
}

function release(symbol: string): void {
  const held = refs.get(symbol);
  if (!held) return;
  held.count -= 1;
  if (held.count <= 0) {
    held.unsub();
    refs.delete(symbol);
  }
}

/** Hold live quotes for `symbols` while the caller is mounted. */
export function useLegQuotes(symbols: string[]): Record<string, OptionQuote> {
  const key = symbols.join(",");
  useEffect(() => {
    const held = key ? key.split(",") : [];
    held.forEach(acquire);
    return () => held.forEach(release);
  }, [key]);
  return useOptionQuoteStore((s) => s.quotes);
}
