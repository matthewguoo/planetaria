import axios from "axios";

// Same-origin by default (vite dev/preview proxy /api to the backend), so
// the app works from any host — laptop, LAN phone, or a tunnel — without
// baking in localhost. VITE_API_URL still overrides for split deployments.
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? "",
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
  max_spread_pct: number;
  entry_ttl_min: number;
  max_trades_per_day: number;
  /** SL breach must persist this long (on the Kalman fair value) before
   * firing; deep breaches fire immediately. 0 = instant. */
  sl_confirm_s: number;
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
  filled_qty?: number | null;
  exit_reason: string | null;
  fill_premium: number | null;
  exit_premium: number | null;
  realized_pnl: number | null;
  mark?: number | null;
  tp_order_id?: string | null;
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

export type PortfolioHistory = {
  timestamps: number[];
  equity: (number | null)[];
  profit_loss: (number | null)[];
  base_value: number | null;
};

export type OpenOrder = {
  id: string;
  symbol: string;
  side: string;
  qty: number | null;
  filled_qty: number;
  type: string;
  limit_price: number | null;
  status: string;
  submitted_at: string | null;
  legs: { symbol: string; side: string; ratio: number }[];
};

export type AccountRisk = {
  asof: number;
  equity: number;
  history_ok: boolean;
  total: {
    risk_dollars: number;
    risk_pct: number | null;
    corr_risk_dollars: number;
    corr_risk_pct: number | null;
    concentration_pct: number;
    open_plans: number;
  };
  greeks: {
    delta_dollars: number;
    beta_weighted_delta_dollars: number;
    vega_per_pt: number;
    theta_per_day: number;
    rho_per_pct: number;
  };
  per_plan: {
    id: string;
    underlying: string;
    status: string;
    risk_dollars: number;
    risk_pct: number | null;
  }[];
  underlyings: {
    symbol: string;
    risk_dollars: number;
    risk_pct: number | null;
    beta_spy: number | null;
    corr_spy: number | null;
    corr_rate: number | null;
    delta_dollars: number;
  }[];
  matrix: { symbols: string[]; rho: (number | null)[][] };
};

export const getAccountRisk = () =>
  api.get<AccountRisk>("/api/account/risk").then((r) => r.data);

export type SystemState = {
  asof: number;
  feed: {
    configured: boolean;
    demo: boolean;
    sources: Record<string, string>;
    stream_age_s: number | null;
    stock_symbols: string[];
    option_symbols: number;
  };
  broker: { configured: boolean; paper: boolean; account_status: string };
  db: { ok: boolean; latency_ms: number | null; engine: string };
  redis: { ok: boolean };
  enforcer: {
    monitors: number;
    monitored_plan_ids: string[];
    ghost_keys: number;
    last_reconcile_age_s: number | null;
    monitors_without_mid: Record<string, string>;
  };
  tasks: Record<string, string>;
};

export type FeedSettings = {
  chain_refresh_s: number;
  positions_poll_s: number;
  account_poll_s: number;
  public_poll_s: number;
  stock_feed: "iex" | "sip";
  option_feed: "indicative" | "opra";
  restart_required_keys: string[];
};

export type PlanEvent = {
  ts: string | null;
  event: string;
  source: string;
  target: string | null;
  applied: boolean;
  detail: string | null;
};

export const getSystemState = () =>
  api.get<SystemState>("/api/system/state").then((r) => r.data);
export const getFeedSettings = () =>
  api.get<FeedSettings>("/api/settings/feed").then((r) => r.data);
export const putFeedSettings = (patch: Partial<FeedSettings>) =>
  api.put<FeedSettings>("/api/settings/feed", patch).then((r) => r.data);
export const getPlanEvents = (planId: string) =>
  api.get<{ events: PlanEvent[] }>(`/api/positions/${planId}/events`).then((r) => r.data.events);

/** Client-side mirror of the server's plan_stop_risk: dollars lost if this
 * plan exits exactly at its stop. */
export function planStopRisk(plan: {
  fill_premium?: number | null;
  entry_limit: number;
  sl_premium: number;
  filled_qty?: number | null;
  qty: number;
}): number {
  const basis = plan.fill_premium ?? plan.entry_limit;
  const qty = plan.filled_qty ?? plan.qty;
  return Math.max(basis - plan.sl_premium, 0) * 100 * Math.max(qty, 0);
}

export const getAccountHistory = (period = "1M", timeframe = "1D") =>
  api
    .get<PortfolioHistory>("/api/account/history", { params: { period, timeframe } })
    .then((r) => r.data);
export const getOpenOrders = () =>
  api.get<{ orders: OpenOrder[] }>("/api/orders/open").then((r) => r.data.orders);
export const cancelOpenOrder = (orderId: string) =>
  api.delete(`/api/orders/${orderId}`).then((r) => r.data);

/** Extract a readable message from an axios error (FastAPI detail). */
export function apiError(err: unknown): string {
  const anyErr = err as { response?: { data?: { detail?: string } }; message?: string };
  return anyErr.response?.data?.detail ?? anyErr.message ?? String(err);
}
