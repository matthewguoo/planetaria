import { useEffect, useRef } from "react";
import { payoffAtExpiry, positionPl, TRADING_HOURS_PER_YEAR } from "../../lib/optionsMath";
import type { Designer } from "../../lib/useDesigner";
import {
  STRATEGY_LABELS,
  useStrategyStore,
  type StrategyKind,
} from "../../store/strategyStore";

function PayoffMini({ designer }: { designer: Designer }) {
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
    if (!designer.legs) return;

    const { legs, spot } = designer;
    const strikeSpan = Math.max(...legs.map((l) => Math.abs(l.strike - spot)), spot * 0.01);
    const lo = spot - strikeSpan * 2.6;
    const hi = spot + strikeSpan * 2.6;
    const n = 120;
    const tau = designer.hoursToExpiry / TRADING_HOURS_PER_YEAR;

    const expiryPl: number[] = [];
    const nowPl: number[] = [];
    for (let i = 0; i <= n; i++) {
      const s = lo + ((hi - lo) * i) / n;
      expiryPl.push(payoffAtExpiry(legs, s) * 100);
      nowPl.push(positionPl(legs, s, tau) * 100);
    }
    const all = [...expiryPl, ...nowPl];
    const minV = Math.min(...all);
    const maxV = Math.max(...all);
    const pad = (maxV - minV || 1) * 0.1;
    const yOf = (v: number) => h - ((v - minV + pad) / (maxV - minV + 2 * pad)) * h;
    const xOf = (i: number) => (i / n) * w;

    // Zero line.
    ctx.strokeStyle = "#333333";
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(0, yOf(0));
    ctx.lineTo(w, yOf(0));
    ctx.stroke();
    ctx.setLineDash([]);

    // Profit/loss fill under expiry curve.
    for (let i = 0; i < n; i++) {
      const v = expiryPl[i];
      ctx.fillStyle = v >= 0 ? "rgba(0,200,83,0.12)" : "rgba(255,23,68,0.12)";
      const y0 = yOf(0);
      const y1 = yOf(v);
      ctx.fillRect(xOf(i), Math.min(y0, y1), w / n + 0.5, Math.abs(y1 - y0));
    }

    const stroke = (values: number[], color: string, dash: number[]) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(dash);
      ctx.beginPath();
      values.forEach((v, i) => (i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v))));
      ctx.stroke();
      ctx.setLineDash([]);
    };
    stroke(expiryPl, "#FFB000", []);
    stroke(nowPl, "#2196F3", [4, 3]);

    // Spot marker.
    const spotX = ((spot - lo) / (hi - lo)) * w;
    ctx.strokeStyle = "#ffffff";
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(spotX, 0);
    ctx.lineTo(spotX, h);
    ctx.stroke();
    ctx.setLineDash([]);
  }, [designer]);

  return <canvas ref={ref} className="h-full w-full" />;
}

export function StrategyPanel({ designer }: { designer: Designer }) {
  const kind = useStrategyStore((s) => s.kind);
  const setKind = useStrategyStore((s) => s.setKind);
  const expiry = useStrategyStore((s) => s.expiry);
  const setExpiry = useStrategyStore((s) => s.setExpiry);
  const chain = useStrategyStore((s) => s.chain);
  const chainError = useStrategyStore((s) => s.chainError);

  return (
    <div className="panel flex min-w-0 flex-col">
      <div className="panel-title">
        STRATEGY{designer.demo ? " · DEMO CHAIN" : ""}
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="flex w-1/2 min-w-0 flex-col gap-1 p-2">
          {(Object.keys(STRATEGY_LABELS) as StrategyKind[]).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={
                "truncate px-2 py-0.5 text-left text-[12px] " +
                (kind === k
                  ? "bg-bb-amber font-semibold text-black"
                  : "text-bb-muted hover:bg-bb-hover hover:text-bb-amber")
              }
            >
              {kind === k ? "● " : "○ "}
              {STRATEGY_LABELS[k]}
            </button>
          ))}
          <div className="mt-1 flex items-center gap-1">
            <span className="text-[10px] text-bb-muted">EXP</span>
            <select
              className="min-w-0 flex-1 border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
              value={expiry ?? ""}
              onChange={(e) => setExpiry(e.target.value)}
            >
              {(chain?.expirations ?? []).map((e) => (
                <option key={e} value={e}>
                  {e} ({dteLabel(e)})
                </option>
              ))}
            </select>
          </div>
          {chainError && (
            <div className="truncate text-[10px] text-bb-loss" title={chainError}>
              CHAIN: {chainError}
            </div>
          )}
        </div>
        <div className="min-w-0 flex-1 border-l border-bb-border p-1">
          {designer.ready ? (
            <PayoffMini designer={designer} />
          ) : (
            <div className="flex h-full items-center justify-center text-[11px] text-bb-muted">
              {chain ? "select strikes" : "loading chain…"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function dteLabel(expiry: string): string {
  const now = new Date();
  const et = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(now);
  const today = new Date(`${et}T00:00:00Z`);
  const exp = new Date(`${expiry}T00:00:00Z`);
  const days = Math.round((exp.getTime() - today.getTime()) / 86_400_000);
  return days <= 0 ? "0DTE" : `${days}DTE`;
}
