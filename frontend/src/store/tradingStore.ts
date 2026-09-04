import { create } from "zustand";
import { exitPositionViewOnAction } from "./uiStore";

export type Quote = { symbol: string; bid: number; ask: number; mid: number; ts: number };

/** A quote older than this cannot anchor spot on its own. */
export const QUOTE_FRESH_MS = 90_000;

export function quoteIsStale(quote: Quote | null, nowMs: number = Date.now()): boolean {
  return !!quote && nowMs - quote.ts > QUOTE_FRESH_MS;
}

/** Freshest defensible spot: the live quote mid while it's fresh, otherwise
 * the fallback (chain spot — which the backend already anchors to the tape
 * via its own quote-vs-bar rule). Guards the frozen-overnight-quote case
 * where the header/model would price 2+ points off the printing bars. */
export function freshSpot(quote: Quote | null, fallback: number, nowMs: number = Date.now()): number {
  if (quote && quote.mid > 0 && !quoteIsStale(quote, nowMs)) return quote.mid;
  return fallback > 0 ? fallback : quote?.mid ?? 0;
}
export type FeedStatus = {
  configured: boolean;
  demo: boolean;
  /** Keyless-mode price source per symbol: real public data vs random walk. */
  sources: Record<string, "public" | "synthetic">;
  redis: boolean;
  stream_age_s: number | null;
  connection: "connecting" | "open" | "down";
};

/** Sub-minute timeframes are rolled server-side from the trade tape (IEX
 * prints on the free tier — real prices, a fraction of the volume) for the
 * symbol on screen; 1m and up are the broker's own bars. */
export type Timeframe = "5s" | "15s" | "30s" | "1m" | "5m" | "15m" | "1h";
export const TIMEFRAMES: Timeframe[] = ["5s", "15s", "30s", "1m", "5m", "15m", "1h"];
export const FAST_TIMEFRAMES: Timeframe[] = ["5s", "15s", "30s"];
export const TF_MS: Record<Timeframe, number> = {
  "5s": 5_000,
  "15s": 15_000,
  "30s": 30_000,
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
};
export function isFastTf(tf: Timeframe): boolean {
  return TF_MS[tf] < 60_000;
}

export type IndicatorToggles = {
  heat: boolean; // P/L heatmap surface
  sim: boolean; // on-chart probability / Monte Carlo stats
  theta: boolean; // theta-sell overlay: expected-move cone + templates
  vwap: boolean;
  ema: boolean;
  bb: boolean;
  sma: boolean; // SMA 20 / 50 / 200
  rsi: boolean; // RSI 14 (oscillator pane)
  macd: boolean; // MACD 12·26·9 (oscillator pane)
};

export type AssetMode = "options" | "equity";

type TradingState = {
  symbol: string;
  tf: Timeframe;
  quote: Quote | null;
  status: FeedStatus;
  indicators: IndicatorToggles;
  /** Which ticket the designer strip shows: options designer vs equity
   * swing ticket. The chart is shared. */
  assetMode: AssetMode;
  setAssetMode: (mode: AssetMode) => void;
  setSymbol: (symbol: string) => void;
  setTf: (tf: Timeframe) => void;
  setQuote: (quote: Quote) => void;
  patchStatus: (patch: Partial<FeedStatus>) => void;
  toggleIndicator: (key: keyof IndicatorToggles) => void;
  /** Show extended-hours bars (pre/after/overnight). Default OFF: RTH-only
   * candles with day boundaries — the discretionary-intraday default. */
  showEth: boolean;
  toggleShowEth: () => void;
};

export const useTradingStore = create<TradingState>((set) => ({
  symbol: "SPY",
  tf: "1m",
  quote: null,
  status: { configured: false, demo: false, sources: {}, redis: false, stream_age_s: null, connection: "connecting" },
  indicators: { heat: true, sim: true, theta: false, vwap: true, ema: false, bb: false, sma: false, rsi: false, macd: false },
  assetMode:
    typeof localStorage !== "undefined" && localStorage.getItem("planetaria.assetMode") === "equity"
      ? "equity"
      : "options",
  setAssetMode: (mode) => {
    exitPositionViewOnAction();
    try {
      localStorage.setItem("planetaria.assetMode", mode);
    } catch {
      // storage unavailable — session-only
    }
    set({ assetMode: mode });
  },
  setSymbol: (symbol) => {
    exitPositionViewOnAction();
    set({ symbol: symbol.toUpperCase(), quote: null });
  },
  setTf: (tf) => set({ tf }),
  setQuote: (quote) => set({ quote }),
  patchStatus: (patch) => set((s) => ({ status: { ...s.status, ...patch } })),
  toggleIndicator: (key) =>
    set((s) => ({ indicators: { ...s.indicators, [key]: !s.indicators[key] } })),
  showEth: typeof localStorage !== "undefined" && localStorage.getItem("planetaria.showEth") === "1",
  toggleShowEth: () =>
    set((s) => {
      const next = !s.showEth;
      try {
        localStorage.setItem("planetaria.showEth", next ? "1" : "0");
      } catch {
        // storage unavailable (private mode) — session-only toggle
      }
      return { showEth: next };
    }),
}));
