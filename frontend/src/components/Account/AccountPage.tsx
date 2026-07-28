/**
 * Full account dashboard for the connected (paper) account — the Alpaca
 * dashboard equivalent: summary stats, equity curve, live positions
 * (managed + untracked), open orders, and closed-trade history. Any
 * position row opens on the chart as a position view.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  adoptPositions,
  apiError,
  cancelOpenOrder,
  closePosition,
  getAccountHistory,
  getHistory,
  getOpenOrders,
  type OpenOrder,
  type Plan,
  type PortfolioHistory,
} from "../../lib/api";
import { useAccountStore } from "../../store/accountStore";
import { useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";

const fmtUsd = (v: number | null | undefined, sign = false) =>
  v === null || v === undefined
    ? "—"
    : `${sign && v > 0 ? "+" : ""}${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

const pnlCls = (v: number | null | undefined) =>
  v === null || v === undefined ? "text-bb-muted" : v >= 0 ? "text-bb-profit" : "text-bb-loss";

function StatCard({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="panel flex flex-col gap-1 p-3">
      <span className="text-[10px] tracking-widest text-bb-muted">{label}</span>
      <span data-numeric className={"text-lg " + (cls ?? "text-white")}>
        {value}
      </span>
    </div>
  );
}

function EquityCurve({ history }: { history: PortfolioHistory | null }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d")!;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = "10px 'SF Mono', Consolas, monospace";

    const points = (history?.equity ?? [])
      .map((v, i) => ({ v, t: history!.timestamps[i] }))
      .filter((p): p is { v: number; t: number } => p.v !== null && p.v > 0);
    if (points.length < 2) {
      ctx.fillStyle = "#666666";
      ctx.textAlign = "center";
      ctx.fillText(
        history === null ? "loading…" : "no portfolio history (broker not configured?)",
        w / 2,
        h / 2,
      );
      return;
    }
    let lo = Infinity;
    let hi = -Infinity;
    for (const p of points) {
      if (p.v < lo) lo = p.v;
      if (p.v > hi) hi = p.v;
    }
    const pad = (hi - lo || hi * 0.01 || 1) * 0.1;
    lo -= pad;
    hi += pad;
    const xOf = (i: number) => (i / (points.length - 1)) * (w - 60) + 4;
    const yOf = (v: number) => h - 16 - ((v - lo) / (hi - lo)) * (h - 24);

    const up = points[points.length - 1].v >= points[0].v;
    const color = up ? "#00C853" : "#FF1744";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach((p, i) => (i === 0 ? ctx.moveTo(xOf(i), yOf(p.v)) : ctx.lineTo(xOf(i), yOf(p.v))));
    ctx.stroke();
    // Fill under.
    ctx.lineTo(xOf(points.length - 1), h - 16);
    ctx.lineTo(xOf(0), h - 16);
    ctx.closePath();
    ctx.fillStyle = up ? "rgba(0,200,83,0.08)" : "rgba(255,23,68,0.08)";
    ctx.fill();
    // End labels.
    ctx.fillStyle = "#999999";
    ctx.textAlign = "left";
    ctx.fillText(`$${Math.round(points[points.length - 1].v).toLocaleString()}`, w - 56, yOf(points[points.length - 1].v));
    ctx.fillStyle = "#666666";
    const first = new Date(points[0].t * 1000);
    const last = new Date(points[points.length - 1].t * 1000);
    ctx.fillText(first.toLocaleDateString("en-US", { month: "short", day: "numeric" }), 4, h - 4);
    ctx.textAlign = "right";
    ctx.fillText(last.toLocaleDateString("en-US", { month: "short", day: "numeric" }), w - 60, h - 4);
  }, [history]);

  return <canvas ref={ref} className="h-full w-full" />;
}

export function AccountPage() {
  const account = useAccountStore((s) => s.account);
  const positions = useAccountStore((s) => s.positions);
  const untracked = useAccountStore((s) => s.untracked);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const viewPosition = useUiStore((s) => s.viewPosition);
  const setSymbol = useTradingStore((s) => s.setSymbol);

  const [history, setHistory] = useState<PortfolioHistory | null>(null);
  const [period, setPeriod] = useState("1M");
  const [orders, setOrders] = useState<OpenOrder[] | null>(null);
  const [closed, setClosed] = useState<Plan[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    const results = await Promise.allSettled([
      getAccountHistory(period, period === "1D" ? "15Min" : "1D"),
      getOpenOrders(),
      getHistory(),
      refreshPositions(),
    ]);
    if (results[0].status === "fulfilled") setHistory(results[0].value);
    else setHistory({ timestamps: [], equity: [], profit_loss: [], base_value: null });
    if (results[1].status === "fulfilled") setOrders(results[1].value);
    else setOrders([]);
    if (results[2].status === "fulfilled") setClosed(results[2].value);
    const failed = results.find((r) => r.status === "rejected");
    if (failed) setError(apiError((failed as PromiseRejectedResult).reason));
  }, [period, refreshPositions]);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const unrealized = positions.reduce((acc, p) => acc + (p.unrealized_pnl ?? 0), 0);
  const openView = (plan: Plan) => {
    setSymbol(plan.underlying);
    viewPosition(plan.id);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto">
      <div className="grid shrink-0 grid-cols-6 gap-px">
        <StatCard label="EQUITY" value={fmtUsd(account?.equity)} />
        <StatCard label="CASH" value={fmtUsd(account?.cash)} />
        <StatCard label="BUYING POWER" value={fmtUsd(account?.buying_power)} />
        <StatCard
          label="DAY REALIZED"
          value={fmtUsd(account?.day_realized_pnl, true)}
          cls={pnlCls(account?.day_realized_pnl)}
        />
        <StatCard label="OPEN UNRLZD" value={fmtUsd(unrealized, true)} cls={pnlCls(unrealized)} />
        <StatCard
          label="STATUS"
          value={`${account?.status ?? "—"} · DT ${account?.daytrade_count ?? "—"}`}
          cls="text-bb-amber"
        />
      </div>

      <div className="panel flex h-48 shrink-0 flex-col">
        <div className="panel-title flex items-center gap-2">
          EQUITY CURVE
          <span className="ml-auto flex gap-px">
            {["1D", "1W", "1M", "3M", "1A"].map((p) => (
              <button
                key={p}
                onClick={() => setPeriod(p)}
                className={
                  "px-1.5 text-[10px] " +
                  (period === p ? "bg-bb-amber font-semibold text-black" : "text-bb-muted hover:text-bb-amber")
                }
              >
                {p}
              </button>
            ))}
          </span>
        </div>
        <div className="min-h-0 flex-1 p-1">
          <EquityCurve history={history} />
        </div>
      </div>

      <div className="panel flex min-h-[10rem] shrink-0 flex-col">
        <div className="panel-title">
          POSITIONS ({positions.length}
          {untracked.length ? ` + ${untracked.length} untracked` : ""})
        </div>
        <table className="w-full border-collapse text-[11px]">
          <thead className="text-[10px] text-bb-muted">
            <tr>
              <th className="px-2 py-1 text-left">POSITION</th>
              <th className="px-2 py-1 text-right">QTY</th>
              <th className="px-2 py-1 text-center">STATUS</th>
              <th className="px-2 py-1 text-right">ENTRY</th>
              <th className="px-2 py-1 text-right">MARK</th>
              <th className="px-2 py-1 text-right">UNRLZD</th>
              <th className="px-2 py-1 text-right">TP / SL</th>
              <th className="px-2 py-1 text-right">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr
                key={p.id}
                className="cursor-pointer border-b border-bb-border/50 hover:bg-bb-hover"
                onClick={() => openView(p)}
                title="View this position on the chart (entry-anchored P/L surface)"
              >
                <td className="px-2 py-1 text-white">
                  {p.underlying}
                  <span className="ml-1 text-[10px] text-bb-muted">
                    {p.legs.map((l) => `${l.side > 0 ? "+" : "−"}${l.ratio || 1}${l.right}${l.strike}`).join(" ")}
                  </span>
                </td>
                <td data-numeric className="px-2 py-1 text-right">{p.filled_qty || p.qty}</td>
                <td className="px-2 py-1 text-center text-[10px] text-bb-muted">{p.status.toUpperCase()}</td>
                <td data-numeric className="px-2 py-1 text-right">{(p.fill_premium ?? p.entry_limit).toFixed(2)}</td>
                <td data-numeric className="px-2 py-1 text-right">{p.mark != null ? p.mark.toFixed(2) : "—"}</td>
                <td data-numeric className={"px-2 py-1 text-right " + pnlCls(p.unrealized_pnl)}>
                  {fmtUsd(p.unrealized_pnl, true)}
                </td>
                <td data-numeric className="px-2 py-1 text-right">
                  <span className="text-bb-profit">{p.tp_premium.toFixed(2)}</span> /{" "}
                  <span className="text-bb-loss">{p.sl_premium.toFixed(2)}</span>
                </td>
                <td className="px-2 py-1 text-right">
                  <button
                    className="border border-bb-amber px-1.5 text-[10px] text-bb-amber hover:bg-bb-amber hover:text-black"
                    onClick={(e) => {
                      e.stopPropagation();
                      openView(p);
                    }}
                  >
                    VIEW
                  </button>
                  <button
                    className="ml-1 border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await closePosition(p.id);
                        await refreshPositions();
                      } catch (err) {
                        setError(apiError(err));
                      }
                    }}
                  >
                    CLOSE
                  </button>
                </td>
              </tr>
            ))}
            {untracked.map((p) => (
              <tr key={p.symbol} className="border-b border-bb-border/50 bg-bb-orange/5">
                <td className="px-2 py-1 text-white">
                  {p.symbol}
                  <span className="ml-1 text-[9px] text-bb-orange">UNTRACKED</span>
                </td>
                <td data-numeric className="px-2 py-1 text-right">{p.qty}</td>
                <td className="px-2 py-1 text-center text-[10px] text-bb-orange">LIVE @ BROKER</td>
                <td data-numeric className="px-2 py-1 text-right">{p.avg_entry_price.toFixed(2)}</td>
                <td data-numeric className="px-2 py-1 text-right">
                  {p.current_price != null ? p.current_price.toFixed(2) : "—"}
                </td>
                <td data-numeric className={"px-2 py-1 text-right " + pnlCls(p.unrealized_pl)}>
                  {fmtUsd(p.unrealized_pl, true)}
                </td>
                <td className="px-2 py-1 text-right text-bb-muted">—</td>
                <td className="px-2 py-1 text-right">
                  {p.occ && (
                    <button
                      className="border border-bb-amber px-1.5 text-[10px] text-bb-amber hover:bg-bb-amber hover:text-black"
                      onClick={async () => {
                        try {
                          await adoptPositions([p.symbol]);
                          await refreshPositions();
                        } catch (err) {
                          setError(apiError(err));
                        }
                      }}
                    >
                      ADOPT
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!positions.length && !untracked.length && (
              <tr>
                <td colSpan={8} className="px-2 py-4 text-center text-bb-muted">
                  no open positions
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel flex min-h-[8rem] shrink-0 flex-col">
        <div className="panel-title">OPEN ORDERS ({orders?.length ?? "…"})</div>
        <table className="w-full border-collapse text-[11px]">
          <thead className="text-[10px] text-bb-muted">
            <tr>
              <th className="px-2 py-1 text-left">ORDER</th>
              <th className="px-2 py-1 text-left">TYPE</th>
              <th className="px-2 py-1 text-right">QTY</th>
              <th className="px-2 py-1 text-right">LIMIT</th>
              <th className="px-2 py-1 text-center">STATUS</th>
              <th className="px-2 py-1 text-right">SUBMITTED</th>
              <th className="px-2 py-1 text-right">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {(orders ?? []).map((o) => (
              <tr key={o.id} className="border-b border-bb-border/50">
                <td className="px-2 py-1 text-white">
                  {o.legs.length
                    ? o.legs.map((l) => `${l.side === "buy" ? "+" : "−"}${l.ratio}× ${l.symbol}`).join(" ")
                    : `${o.side.toUpperCase()} ${o.symbol}`}
                </td>
                <td className="px-2 py-1 text-bb-muted">{o.type.toUpperCase()}</td>
                <td data-numeric className="px-2 py-1 text-right">
                  {o.filled_qty > 0 ? `${o.filled_qty}/` : ""}
                  {o.qty ?? "—"}
                </td>
                <td data-numeric className="px-2 py-1 text-right">
                  {o.limit_price != null ? o.limit_price.toFixed(2) : "MKT"}
                </td>
                <td className="px-2 py-1 text-center text-[10px] text-bb-muted">{o.status.toUpperCase()}</td>
                <td data-numeric className="px-2 py-1 text-right text-bb-muted">
                  {o.submitted_at
                    ? new Date(o.submitted_at).toLocaleTimeString("en-US", {
                        timeZone: "America/New_York",
                        hour: "2-digit",
                        minute: "2-digit",
                        hour12: false,
                      }) + " ET"
                    : "—"}
                </td>
                <td className="px-2 py-1 text-right">
                  <button
                    className="border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
                    onClick={async () => {
                      try {
                        await cancelOpenOrder(o.id);
                        setOrders(await getOpenOrders());
                      } catch (err) {
                        setError(apiError(err));
                      }
                    }}
                  >
                    CANCEL
                  </button>
                </td>
              </tr>
            ))}
            {orders && !orders.length && (
              <tr>
                <td colSpan={7} className="px-2 py-4 text-center text-bb-muted">
                  no open orders
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel flex min-h-[8rem] flex-1 flex-col">
        <div className="panel-title">
          CLOSED TRADES ({closed?.length ?? "…"}) ·{" "}
          <span data-numeric className={pnlCls((closed ?? []).reduce((a, t) => a + (t.realized_pnl ?? 0), 0))}>
            TOTAL {fmtUsd((closed ?? []).reduce((a, t) => a + (t.realized_pnl ?? 0), 0), true)}
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <tbody>
              {(closed ?? []).map((t) => (
                <tr key={t.id} className="border-b border-bb-border/50">
                  <td className="px-2 py-1 text-bb-muted">{t.created_at.slice(0, 10)}</td>
                  <td className="px-2 py-1 text-white">
                    {t.underlying}
                    <span className="ml-1 text-[10px] text-bb-muted">
                      {t.legs.map((l) => `${l.side > 0 ? "+" : "−"}${l.strike}${l.right}`).join(" ")}
                    </span>
                  </td>
                  <td data-numeric className="px-2 py-1 text-right">×{t.qty}</td>
                  <td className="px-2 py-1 text-center text-[10px] text-bb-muted">
                    {(t.exit_reason ?? t.status).toUpperCase()}
                  </td>
                  <td data-numeric className={"px-2 py-1 text-right " + pnlCls(t.realized_pnl)}>
                    {fmtUsd(t.realized_pnl, true)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {error && <div className="px-2 py-1 text-[10px] text-bb-loss">{error}</div>}
    </div>
  );
}
