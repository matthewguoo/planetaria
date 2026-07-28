import axios from "axios";

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? "http://localhost:8000",
  timeout: 10_000,
});

export type SymbolHit = { symbol: string; name: string };

export async function searchSymbols(q: string): Promise<SymbolHit[]> {
  const { data } = await api.get<{ results: SymbolHit[] }>("/api/symbols/search", {
    params: { q },
  });
  return data.results;
}

export type RiskSettings = {
  max_loss_pct: number;
  daily_loss_pct: number;
  max_positions: number;
  bp_cap_pct: number;
  default_tp_pct: number;
  default_sl_pct: number;
  time_stop_et: string;
  expiry_time_stop_et: string;
};

export type Account = {
  equity: number;
  cash: number;
  buying_power: number;
  daytrade_count: number;
  status: string;
  paper: boolean;
  risk: RiskSettings;
  day_realized_pnl: number;
};

export type PlanLeg = {
  symbol: string;
  right: "C" | "P";
  strike: number;
  expiry: string;
  side: number;
  ratio: number;
  entry: number;
  iv: number;
};

export type Plan = {
  id: string;
  created_at: string;
  underlying: string;
  strategy: string;
  legs: PlanLeg[];
  qty: number;
  entry_limit: number;
  tp_premium: number;
  sl_premium: number;
  time_stop_utc: string;
  status: string;
  exit_reason: string | null;
  fill_premium: number | null;
  exit_premium: number | null;
  realized_pnl: number | null;
  mark?: number | null;
  unrealized_pnl?: number | null;
  quote_stale?: boolean;
};

export type UntrackedPosition = {
  symbol: string;
  qty: number;
  side: number;
  asset_class: "option" | "stock";
  avg_entry_price: number;
  current_price: number | null;
  market_value: number | null;
  unrealized_pl: number | null;
  occ: { underlying: string; expiry: string; right: "C" | "P"; strike: number } | null;
};

export type PositionsPayload = {
  positions: Plan[];
  untracked: UntrackedPosition[];
  untracked_error?: string;
};

export const getAccount = () => api.get<Account>("/api/account").then((r) => r.data);
export const putRisk = (patch: Partial<RiskSettings>) =>
  api.put<RiskSettings>("/api/settings/risk", patch).then((r) => r.data);
export const getPositions = () =>
  api.get<PositionsPayload>("/api/positions").then((r) => r.data);
export const adoptPositions = (symbols: string[]) =>
  api.post<{ adopted: Plan[] }>("/api/positions/adopt", { symbols }).then((r) => r.data.adopted);
export const getHistory = () =>
  api.get<{ trades: Plan[] }>("/api/history").then((r) => r.data.trades);
export const postOrder = (payload: object) =>
  api.post<Plan>("/api/orders", payload).then((r) => r.data);
export const closePosition = (planId: string) =>
  api.post(`/api/positions/${planId}/close`).then((r) => r.data);
export const flattenAll = () => api.post("/api/positions/flatten").then((r) => r.data);
export const tightenExits = (
  planId: string,
  patch: { tp_premium?: number; sl_premium?: number; time_stop_utc?: string },
) => api.patch<Plan>(`/api/positions/${planId}/exits`, patch).then((r) => r.data);

/** Extract a readable message from an axios error (FastAPI detail). */
export function apiError(err: unknown): string {
  const anyErr = err as { response?: { data?: { detail?: string } }; message?: string };
  return anyErr.response?.data?.detail ?? anyErr.message ?? String(err);
}
