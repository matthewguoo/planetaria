/**
 * Upper-left on-chart HUD: overlay/sim switches plus the probability &
 * Monte Carlo readout, rendered as an HTML layer over the canvas so the
 * numbers live where the eyes are — on the chart, not in a bottom panel.
 * The container ignores pointer events; only its controls capture them, so
 * chart pan/zoom/drag pass straight through.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  getPlanEvents,
  getSystemState,
  type PlanEvent,
  type SystemState,
} from "../../lib/api";
import { computeAtr, realizedVolAnnualized } from "../../lib/indicators";
import type { McResult } from "../../lib/mcSim";
import { bsThetaPerDay, structuralMaxLoss, TRADING_HOURS_PER_YEAR } from "../../lib/optionsMath";
import { useMonteCarlo, type McInputs } from "../../lib/useMonteCarlo";
import type { Designer } from "../../lib/useDesigner";
import { useAccountStore } from "../../store/accountStore";
import { THETA_TEMPLATES, useStrategyStore } from "../../store/strategyStore";
import { TF_MS, useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import type { Bars } from "./scales";

const TOGGLES = [
  { key: "heat", label: "HEAT", title: "P/L heatmap surface (price × time)" },
  { key: "sim", label: "SIM", title: "Probability + Monte Carlo readout" },
  { key: "theta", label: "THETA", title: "Theta-sell overlay: expected-move cone + delta-targeted premium-selling templates" },
  { key: "vwap", label: "VWAP", title: "Session-anchored VWAP" },
  { key: "ema", label: "EMA", title: "EMA 9 / 21" },
  { key: "bb", label: "BB", title: "Bollinger 20 × 2σ" },
] as const;

const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(0)}%`);
const usd = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(0)}`;

function StatRow({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-bb-muted">{label}</span>
      <span data-numeric className={cls ?? "text-white"}>
        {value}
      </span>
    </div>
  );
}

function etCountdown(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso) - Date.now();
  if (ms <= 0) return "due";
  const m = Math.floor(ms / 60000);
  return m >= 90 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
}

/** "What is the enforcer doing for THIS position right now" — the live
 * conditionals of the bracket, shown while viewing a position on the chart.
 * Polls system state (monitor/health) and the plan's lifecycle journal. */
function EnforcerBlock({ planId }: { planId: string }) {
  const plan = useAccountStore((s) => s.positions.find((p) => p.id === planId) ?? null);
  const [sys, setSys] = useState<SystemState | null>(null);
  const [events, setEvents] = useState<PlanEvent[]>([]);

  useEffect(() => {
    let live = true;
    const tick = () => {
      getSystemState().then((s) => live && setSys(s)).catch(() => {});
      getPlanEvents(planId).then((e) => live && setEvents(e.slice(0, 3))).catch(() => {});
    };
    tick();
    const id = window.setInterval(tick, 5000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [planId]);

  if (!plan) return null;
  const monitored = sys?.enforcer.monitored_plan_ids.includes(planId) ?? null;
  const health = sys?.enforcer.monitors_without_mid?.[planId];
  const mark = plan.mark;
  const span = Math.abs(plan.tp_premium - plan.sl_premium) || 1;
  const slDist = mark != null ? ((mark - plan.sl_premium) / span) * 100 : null;

  return (
    <div
      className="pointer-events-none flex flex-col gap-0.5 border border-bb-amber/50 bg-black/80 px-1.5 py-1"
      title="Live bracket state: exactly what the exit enforcer is watching and will do for this position"
    >
      <div className="flex justify-between gap-2">
        <span className="tracking-widest text-bb-amber">ENFORCER</span>
        <span
          className={
            monitored === null
              ? "text-bb-muted"
              : monitored && !health
                ? "text-bb-profit"
                : "text-bb-orange"
          }
        >
          {monitored === null ? "…" : !monitored ? "⚠ NO MONITOR" : health ? `⚠ ${health}` : "● WATCHING"}
        </span>
      </div>
      <StatRow
        label="MARK vs SL/TP"
        value={
          mark != null
            ? `${mark.toFixed(2)} in [${plan.sl_premium.toFixed(2)} … ${plan.tp_premium.toFixed(2)}]`
            : "—"
        }
        cls={slDist != null && slDist < 25 ? "text-bb-orange" : "text-white"}
      />
      {slDist != null && (
        <StatRow
          label="TO STOP"
          value={`${Math.max(slDist, 0).toFixed(0)}% of bracket`}
          cls={slDist < 25 ? "text-bb-loss" : "text-bb-muted"}
        />
      )}
      <StatRow
        label="TP"
        value={plan.tp_order_id ? "RESTING @ BROKER" : "software trigger"}
        cls={plan.tp_order_id ? "text-bb-profit" : "text-bb-muted"}
      />
      <StatRow
        label="SL"
        value="median mid + dwell → ladder −2%→−6%→MKT"
        cls="text-bb-muted"
      />
      <StatRow label="TIME STOP" value={etCountdown(plan.time_stop_utc)} cls="text-bb-orange" />
      {events.map((e, i) => (
        <div key={i} className="truncate text-[9px] leading-tight text-bb-muted">
          {e.ts ? e.ts.slice(11, 19) : "—"} {e.event}
          {e.target ? `→${e.target}` : ""} {e.applied ? "" : "(dropped)"}
        </div>
      ))}
    </div>
  );
}

/** Theta-sell block: delta-targeted templates + credit-selling metrics.
 * Templates resolve short strikes by |delta| from the LIVE chain.
 * Exported so the mobile layout can host it in a tab below the chart. */
export function ThetaBlock({ designer }: { designer: Designer }) {
  const applyThetaTemplate = useStrategyStore((s) => s.applyThetaTemplate);
  const chain = useStrategyStore((s) => s.chain);
  const [failed, setFailed] = useState<string | null>(null);

  const isCredit = designer.ready && designer.entry < 0;
  const credit = isCredit ? Math.abs(designer.entry) * 100 : 0;
  const structural = designer.legs ? structuralMaxLoss(designer.legs) : null;
  const maxLoss = structural !== null ? Math.abs(structural) * 100 : null;
  const width = maxLoss !== null ? maxLoss + credit : null; // wing width $
  const tau = designer.hoursToExpiry / TRADING_HOURS_PER_YEAR;
  // Per contract SET (like CREDIT/SET) — qty may still be 0 pre-sizing.
  const thetaDay = designer.legs
    ? designer.legs.reduce(
        (acc, leg) =>
          acc +
          leg.side * leg.qty * 100 * bsThetaPerDay(designer.spot, leg.strike, tau, leg.iv, leg.right),
        0,
      )
    : 0;
  // Expected move to expiry (1σ) vs the nearest short strike.
  const sigma = designer.probabilities?.sigmaUsed ?? 0;
  const em = designer.spot > 0 && sigma > 0 ? designer.spot * sigma * Math.sqrt(Math.max(tau, 0)) : 0;
  const shortStrikes = (designer.legs ?? []).filter((l) => l.side < 0).map((l) => l.strike);
  const minShortDist = shortStrikes.length
    ? Math.min(...shortStrikes.map((k) => Math.abs(k - designer.spot)))
    : null;
  const insideEm = minShortDist !== null && em > 0 && minShortDist < em;

  return (
    <div className="pointer-events-auto flex flex-col gap-0.5 border border-bb-border/60 bg-black/75 px-1.5 py-1">
      <div className="flex flex-wrap gap-px">
        {THETA_TEMPLATES.map((t) => (
          <button
            key={t.id}
            disabled={!chain}
            title={t.title}
            onClick={() => setFailed(applyThetaTemplate(t.id) ? null : t.label)}
            className="border border-bb-border px-1 py-0.5 text-bb-muted hover:text-bb-amber disabled:opacity-30"
          >
            {t.label}
          </button>
        ))}
      </div>
      {failed && (
        <span className="text-bb-loss">couldn't resolve {failed} — no OTM deltas in chain</span>
      )}
      {isCredit ? (
        <>
          <StatRow label="CREDIT/SET" value={`$${credit.toFixed(0)}`} cls="text-bb-profit" />
          {width !== null && width > 0 && (
            <StatRow
              label="CREDIT/WIDTH"
              value={`${((credit / width) * 100).toFixed(0)}%`}
              cls={credit / width >= 0.25 ? "text-bb-profit" : "text-bb-orange"}
            />
          )}
          <StatRow
            label="THETA/DAY/SET"
            value={usd(thetaDay)}
            cls={thetaDay >= 0 ? "text-bb-profit" : "text-bb-loss"}
          />
          {em > 0 && (
            <StatRow label="EXP MOVE ±" value={`$${em.toFixed(2)}`} cls="text-bb-amber" />
          )}
          {insideEm && (
            <span className="leading-tight text-bb-orange" title="A short strike sits closer than one expected move (1σ to expiry) — high touch probability">
              ⚠ SHORT STRIKE INSIDE EXPECTED MOVE
            </span>
          )}
        </>
      ) : (
        <span className="text-bb-muted">pick a template — current position is not net credit</span>
      )}
    </div>
  );
}

export function ChartHud({
  designer,
  barsRef,
  variant = "full",
}: {
  designer: Designer;
  barsRef: React.RefObject<Bars>;
  /** "chips": toggles + one legend line only — the phone layout hosts the
   * MC/theta data in tabs below the chart instead of floating over it. */
  variant?: "full" | "chips";
}) {
  const indicators = useTradingStore((s) => s.indicators);
  const toggleIndicator = useTradingStore((s) => s.toggleIndicator);
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const tf = useTradingStore((s) => s.tf);
  const volShift = useStrategyStore((s) => s.volShift);
  const setVolShift = useStrategyStore((s) => s.setVolShift);
  const skewBeta = useStrategyStore((s) => s.skewBeta);
  const setSkewBeta = useStrategyStore((s) => s.setSkewBeta);
  const [mc, setMc] = useState<McResult | null>(null);
  const mcRef = useRef<McResult | null>(null);
  mcRef.current = mc;

  const mcInputs: McInputs | null = useMemo(() => {
    if (variant !== "full" || !indicators.sim || !designer.ready || !designer.legs) return null;
    return {
      legs: designer.legs,
      entry: designer.entry,
      tpPremium: designer.tpPremium,
      slPremium: designer.slPremium,
      hoursToExpiry: designer.hoursToExpiry,
      timeStopHours: designer.timeStopHours,
      spot: designer.spot,
      smiles: designer.smiles,
      volShift,
      skewBeta,
      frictionPerSet: designer.frictionPerSet,
    };
  }, [designer, volShift, skewBeta, indicators.sim, variant]);

  useMonteCarlo(mcInputs, setMc);

  // Bars context (ATR / RV) — read from the render ref; designer updates
  // arrive often enough that this stays fresh without its own ticker.
  const bars = barsRef.current;
  const tfMinutes = TF_MS[tf] / 60000;
  const atr = bars && bars.n ? computeAtr(bars) : 0;
  const rv = bars && bars.n ? realizedVolAnnualized(bars, 30, tfMinutes) : 0;
  const iv = designer.legs ? (designer.probabilities?.sigmaUsed ?? 0) : 0;
  const ivRv = iv > 0 && rv > 0 ? iv - rv : null;

  const p = designer.probabilities;

  return (
    <div className="pointer-events-none absolute left-1.5 top-1.5 z-10 flex w-56 flex-col gap-1 text-[10px]">
      <div className="pointer-events-auto flex flex-wrap gap-px">
        {TOGGLES.map(({ key, label, title }) => (
          <button
            key={key}
            onClick={() => toggleIndicator(key)}
            title={title}
            className={
              "border border-bb-border px-1.5 py-0.5 " +
              (indicators[key]
                ? "bg-bb-hover font-semibold text-bb-amber"
                : "bg-black/70 text-bb-muted hover:text-bb-amber")
            }
          >
            {label}
          </button>
        ))}
        <label
          className="ml-1 flex items-center gap-0.5 border border-bb-border bg-black/70 px-1 text-bb-muted"
          title="IV shock: parallel scenario vol shift for surface, contours, and sim"
        >
          IV
          <input
            data-numeric
            type="number"
            step={5}
            min={-50}
            max={50}
            className="w-9 bg-transparent text-right text-bb-amber outline-none"
            value={Math.round(volShift * 100)}
            onChange={(e) => setVolShift(Number(e.target.value) / 100)}
            aria-label="IV shock percent"
          />
          %
        </label>
        <label
          className="flex items-center gap-0.5 border border-bb-border bg-black/70 px-1 text-bb-muted"
          title="Skew beta: chain-derived directional vol response (selloff => vols up)"
        >
          <input
            type="checkbox"
            checked={skewBeta}
            onChange={(e) => setSkewBeta(e.target.checked)}
            aria-label="Apply skew beta"
          />
          β
        </label>
      </div>

      <div className="pointer-events-none flex items-center gap-1 bg-black/70 px-1.5 py-0.5 text-bb-muted">
        {ivRv !== null && (
          <span
            className={
              "h-2.5 w-1 " + (ivRv > 0.03 ? "bg-bb-orange" : ivRv < -0.03 ? "bg-bb-profit" : "bg-bb-muted")
            }
            title="IV−RV: orange = premium rich (hurts buyers), green = cheap"
          />
        )}
        <span data-numeric>
          ATR {atr ? atr.toFixed(2) : "—"} · RV {rv ? (rv * 100).toFixed(1) : "—"}%
          {iv > 0 && ` · IV ${(iv * 100).toFixed(1)}%`}
          {ivRv !== null && ` · ${ivRv >= 0 ? "+" : ""}${(ivRv * 100).toFixed(1)}pt`}
        </span>
      </div>

      {viewingPlanId && <EnforcerBlock planId={viewingPlanId} />}

      {variant === "full" && indicators.theta && <ThetaBlock designer={designer} />}

      {variant === "full" && indicators.sim && designer.ready && p && (
        <div
          className="pointer-events-none flex flex-col gap-0.5 border border-bb-border/60 bg-black/75 px-1.5 py-1"
          title="Monte Carlo: 2000 paths with the enforcer's exact exit rules, net of spread friction. Analytic rows: risk-neutral GBM."
        >
          {mc ? (
            <>
              <StatRow
                label="MC EV/SET"
                value={usd(mc.evPerSet)}
                cls={mc.evPerSet >= 0 ? "text-bb-profit" : "text-bb-loss"}
              />
              <StatRow label="WIN" value={pct(mc.winRate)} />
              <StatRow
                label="EXITS"
                value={`TP ${Math.round(mc.pTp * 100)}·SL ${Math.round(mc.pSl * 100)}·T ${Math.round((mc.pTime + mc.pExpiry) * 100)}`}
              />
              <StatRow label="P5·P50·P95" value={`${usd(mc.p5)}·${usd(mc.p50)}·${usd(mc.p95)}`} />
              <StatRow
                label="AVG TIME"
                value={
                  mc.avgMinutesInTrade >= 90
                    ? `${(mc.avgMinutesInTrade / 60).toFixed(1)}h`
                    : `${Math.round(mc.avgMinutesInTrade)}m`
                }
              />
            </>
          ) : (
            <StatRow label="MC" value="simulating…" cls="text-bb-muted" />
          )}
          <StatRow label="P(PROFIT)" value={pct(p.pProfitExpiry)} />
          <StatRow
            label="TOUCH TP/SL"
            value={`${pct(p.pTouchTp)} / ${pct(p.pTouchSl)}`}
          />
          <StatRow label="R:R" value={p.rr === null ? "—" : `${p.rr.toFixed(2)}:1`} />
        </div>
      )}
    </div>
  );
}
