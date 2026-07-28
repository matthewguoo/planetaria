import { create } from "zustand";

export type Quote = { symbol: string; bid: number; ask: number; mid: number; ts: number };
export type FeedStatus = {
  configured: boolean;
  demo: boolean;
  redis: boolean;
  stream_age_s: number | null;
  connection: "connecting" | "open" | "down";
};

export type Timeframe = "1m" | "5m" | "15m" | "1h";
export const TIMEFRAMES: Timeframe[] = ["1m", "5m", "15m", "1h"];
export const TF_MS: Record<Timeframe, number> = {
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
};

type TradingState = {
  symbol: string;
  tf: Timeframe;
  quote: Quote | null;
  status: FeedStatus;
  setSymbol: (symbol: string) => void;
  setTf: (tf: Timeframe) => void;
  setQuote: (quote: Quote) => void;
  patchStatus: (patch: Partial<FeedStatus>) => void;
};

export const useTradingStore = create<TradingState>((set) => ({
  symbol: "SPY",
  tf: "1m",
  quote: null,
  status: { configured: false, demo: false, redis: false, stream_age_s: null, connection: "connecting" },
  setSymbol: (symbol) => set({ symbol: symbol.toUpperCase(), quote: null }),
  setTf: (tf) => set({ tf }),
  setQuote: (quote) => set({ quote }),
  patchStatus: (patch) => set((s) => ({ status: { ...s.status, ...patch } })),
}));
