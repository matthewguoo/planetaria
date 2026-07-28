import { useState } from "react";
import { apiError, postOrder } from "../../lib/api";
import type { Designer } from "../../lib/useDesigner";
import { useAccountStore } from "../../store/accountStore";
import { useStrategyStore } from "../../store/strategyStore";
import { useTradingStore } from "../../store/tradingStore";

function etToUtcIso(timeEt: string): string {
  // Today at HH:MM ET -> UTC ISO. Uses the ET offset implied by current date.
  const now = new Date();
  const etNow = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const offsetMs = now.getTime() - etNow.getTime();
  const [h, m] = timeEt.split(":").map(Number);
  const etTarget = new Date(etNow);
  etTarget.setHours(h, m, 0, 0);
  return new Date(etTarget.getTime() + offsetMs).toISOString();
}

export function OrderPanel({ designer }: { designer: Designer }) {
  const symbol = useTradingStore((s) => s.symbol);
  const kind = useStrategyStore((s) => s.kind);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const setTpPct = useStrategyStore((s) => s.setTpPct);
  const setSlPct = useStrategyStore((s) => s.setSlPct);
  const timeStopEt = useStrategyStore((s) => s.timeStopEt);
  const setTimeStopEt = useStrategyStore((s) => s.setTimeStopEt);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);

  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<string | null>(null);

  const canTrade =
    designer.ready && designer.qty > 0 && !designer.demo && designer.instantExit === null;

  const submit = async () => {
    if (!designer.legs) return;
    setSubmitting(true);
    setError(null);
    try {
      const plan = await postOrder({
        underlying: symbol,
        strategy: kind,
        legs: designer.legs.map((leg) => ({
          symbol: leg.symbol,
          right: leg.right,
          strike: leg.strike,
          expiry: leg.expiry,
          side: leg.side,
          ratio: 1,
          entry: leg.entry,
          iv: leg.iv,
        })),
        qty: designer.qty,
        entry_limit: Number(designer.entry.toFixed(2)),
        tp_premium: Number(designer.tpPremium!.toFixed(2)),
        sl_premium: Number(designer.slPremium!.toFixed(2)),
        time_stop_utc: etToUtcIso(timeStopEt),
      });
      setPlaced(plan.id);
      setConfirming(false);
      void refreshPositions();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="panel relative flex min-w-0 flex-col">
      <div className="panel-title">ORDER</div>
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-bb-muted">TP</span>
          <span className="flex items-center gap-1">
            <input
              data-numeric
              type="number"
              step={10}
              min={5}
              max={1000}
              className="w-14 border border-bb-border bg-black px-1 py-0.5 text-right text-[12px] text-bb-profit outline-none focus:border-bb-amber"
              value={Math.round(tpPct * 100)}
              onChange={(e) => setTpPct(Number(e.target.value) / 100)}
              aria-label="Take profit percent"
            />
            <span className="text-[10px] text-bb-muted">%</span>
            <span data-numeric className="w-14 text-right text-[11px] text-bb-profit">
              {designer.tpPremium != null ? designer.tpPremium.toFixed(2) : "—"}
            </span>
          </span>
        </div>
        <div
          className="-mt-1 text-right text-[9px] text-bb-muted"
          title="Underlying price where the position premium hits TP (green boundary on the chart)"
        >
          executes ≈ @{designer.probabilities?.tpBarrier?.toFixed(2) ?? "—"}
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-bb-muted">SL</span>
          <span className="flex items-center gap-1">
            <input
              data-numeric
              type="number"
              step={5}
              min={5}
              max={95}
              className="w-14 border border-bb-border bg-black px-1 py-0.5 text-right text-[12px] text-bb-loss outline-none focus:border-bb-amber"
              value={Math.round(slPct * 100)}
              onChange={(e) => setSlPct(Number(e.target.value) / 100)}
              aria-label="Stop loss percent"
            />
            <span className="text-[10px] text-bb-muted">%</span>
            <span data-numeric className="w-14 text-right text-[11px] text-bb-loss">
              {designer.slPremium != null ? designer.slPremium.toFixed(2) : "—"}
            </span>
          </span>
        </div>
        <div
          className="-mt-1 text-right text-[9px] text-bb-muted"
          title="Underlying price where the position premium hits SL (red boundary on the chart)"
        >
          executes ≈ @{designer.probabilities?.slBarrier?.toFixed(2) ?? "—"}
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-bb-muted">TIME STOP</span>
          <span className="flex items-center gap-1">
            <input
              type="time"
              className="border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-orange outline-none focus:border-bb-amber"
              value={timeStopEt}
              onChange={(e) => e.target.value && setTimeStopEt(e.target.value)}
              aria-label="Time stop (ET)"
            />
            <span className="text-[10px] text-bb-muted">ET</span>
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[11px] text-bb-muted">
            ENTRY {designer.ready && designer.entry < 0 ? "CREDIT" : "LIMIT"}
          </span>
          <span data-numeric className={"text-[12px] " + (designer.entry < 0 ? "text-bb-profit" : "text-bb-amber")}>
            {designer.ready ? `${Math.abs(designer.entry).toFixed(2)} × ${designer.qty}` : "—"}
          </span>
        </div>

        <div className="mt-auto flex gap-1">
          <button
            className="btn-primary flex-1 text-[12px]"
            disabled={!canTrade || submitting}
            onClick={() => {
              setError(null);
              setPlaced(null);
              setConfirming(true);
            }}
            title={designer.demo ? "Demo chain - configure Alpaca keys to trade" : "Paper trade"}
          >
            PAPER
          </button>
          <button
            className="flex-1 cursor-not-allowed border border-bb-border text-[12px] text-bb-muted"
            disabled
            title="Live trading is locked until the system is validated on paper"
          >
            LIVE 🔒
          </button>
        </div>
        {designer.instantExit && (
          <div
            className="text-[10px] leading-tight text-bb-loss"
            title="Current model value of the position is already at/beyond this exit level. Filling now would be closed by the exit enforcer immediately — adjust TP/SL or wait for quotes to refresh."
          >
            ⚠ {designer.instantExit.toUpperCase()} already breached at current price — order
            would exit instantly
          </div>
        )}
        {placed && (
          <div className="truncate text-[10px] text-bb-profit">plan {placed.slice(0, 8)} submitted</div>
        )}
        {error && !confirming && (
          <div className="text-[10px] leading-tight text-bb-loss" title={error}>
            {error}
          </div>
        )}
      </div>

      {confirming && designer.ready && (
        <div className="absolute inset-0 z-30 flex flex-col gap-1 bg-black/95 p-2 text-[11px]">
          <div className="text-bb-amber">CONFIRM PAPER ORDER</div>
          <div className="text-white">
            {symbol} {kind.replace("_", " ").toUpperCase()} × {designer.qty}
          </div>
          <div data-numeric className="text-bb-muted">
            {designer.entry < 0 ? "credit" : "debit"} {designer.entry.toFixed(2)} · TP{" "}
            {designer.tpPremium!.toFixed(2)} · SL {designer.slPremium!.toFixed(2)}
          </div>
          <div data-numeric className="text-bb-muted">
            max loss ${designer.sizing ? (designer.sizing.perContractRisk * designer.qty).toFixed(0) : "—"} ·
            stop {timeStopEt} ET
          </div>
          <div className="text-[10px] text-bb-orange">
            Exits are enforced server-side. No manual override once filled — only tightening.
          </div>
          {error && (
            <div className="max-h-16 overflow-y-auto text-[10px] leading-tight text-bb-loss">{error}</div>
          )}
          <div className="mt-auto flex gap-1">
            <button className="btn-primary flex-1" disabled={submitting} onClick={submit}>
              {submitting ? "SUBMITTING…" : "CONFIRM"}
            </button>
            <button
              className="btn-ghost flex-1"
              disabled={submitting}
              onClick={() => setConfirming(false)}
            >
              CANCEL
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
